"""Tests app/mcp_server.py's per-tenant get_service() in HTTP mode: two
different onboarded tenants (identified by the current bearer token's
`subject`, see app/oauth_provider.py's _issue_tokens) must resolve to two
distinct ZoteroService instances, each backed by its own Zotero library --
never a shared one.

Follows the existing pattern of monkeypatching pyzotero.zotero.Zotero
(see test_zotero_service.py/test_mcp_server.py's
test_move_item_to_different_library_tool) rather than hitting a real
Zotero API, and importing app.mcp_server fresh under $PORT (see
test_mcp_server_oauth.py) since get_service()'s per-tenant branch is only
active in HTTP mode.
"""

from __future__ import annotations

import importlib
import sys

import pytest
from cryptography.fernet import Fernet
from mcp.server.auth.provider import AccessToken

from tests.fakes import FakeZotero


@pytest.fixture
def fake_zotero_instances(monkeypatch):
    """Keyed by library_id, so each tenant's ZoteroService wraps its own
    FakeZotero rather than sharing one."""
    instances: dict[str, FakeZotero] = {}

    def factory(library_id, library_type, api_key):
        if library_id not in instances:
            instances[library_id] = FakeZotero(
                library_id=library_id, library_type=library_type
            )
        return instances[library_id]

    import app.zotero_service as svc_mod

    monkeypatch.setattr(svc_mod.zotero, "Zotero", factory)
    return instances


@pytest.fixture
def http_module(tmp_path, monkeypatch):
    monkeypatch.setenv("PORT", "8299")
    monkeypatch.delenv("ZOTERO_LIBRARY_ID", raising=False)
    monkeypatch.delenv("ZOTERO_API_KEY", raising=False)
    monkeypatch.setenv("MCP_TOKEN_STORE_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("MCP_PUBLIC_URL", "https://example.test")
    monkeypatch.setenv("MCP_DATA_DIR", str(tmp_path))

    sys.modules.pop("app.mcp_server", None)
    module = importlib.import_module("app.mcp_server")
    yield module
    sys.modules.pop("app.mcp_server", None)


def _set_current_subject(module, monkeypatch, subject: str) -> None:
    token = AccessToken(
        token="tok", client_id="client-1", scopes=[], expires_at=None, subject=subject
    )
    monkeypatch.setattr(module, "get_access_token", lambda: token)


def test_get_service_is_per_tenant(http_module, fake_zotero_instances, monkeypatch):
    http_module._token_store.put_tenant(
        "111", {"library_id": "111", "library_type": "user", "api_key": "key-a"}
    )
    http_module._token_store.put_tenant(
        "222", {"library_id": "222", "library_type": "user", "api_key": "key-b"}
    )

    _set_current_subject(http_module, monkeypatch, "111")
    service_a = http_module.get_service()
    service_a.zot.seed_item("book", {"title": "Tenant A's book"})

    _set_current_subject(http_module, monkeypatch, "222")
    service_b = http_module.get_service()
    service_b.zot.seed_item("book", {"title": "Tenant B's book"})

    assert service_a is not service_b
    assert service_a.zot is not service_b.zot
    assert service_a.api_key == "key-a"
    assert service_b.api_key == "key-b"


def test_tool_calls_only_see_own_tenant_data(
    http_module, fake_zotero_instances, monkeypatch
):
    http_module._token_store.put_tenant(
        "111", {"library_id": "111", "library_type": "user", "api_key": "key-a"}
    )
    http_module._token_store.put_tenant(
        "222", {"library_id": "222", "library_type": "user", "api_key": "key-b"}
    )

    _set_current_subject(http_module, monkeypatch, "111")
    http_module.get_service().zot.seed_item("book", {"title": "Only Tenant A"})

    _set_current_subject(http_module, monkeypatch, "222")
    http_module.get_service().zot.seed_item("book", {"title": "Only Tenant B"})

    results_b = http_module.search_items()
    assert [r["title"] for r in results_b] == ["Only Tenant B"]

    _set_current_subject(http_module, monkeypatch, "111")
    results_a = http_module.search_items()
    assert [r["title"] for r in results_a] == ["Only Tenant A"]


def test_get_service_rebuilds_cache_on_rotated_api_key(
    http_module, fake_zotero_instances, monkeypatch
):
    http_module._token_store.put_tenant(
        "111", {"library_id": "111", "library_type": "user", "api_key": "old-key"}
    )
    _set_current_subject(http_module, monkeypatch, "111")
    first = http_module.get_service()
    assert first.api_key == "old-key"

    # Tenant re-logs in with a rotated key (upsert -- see complete_login).
    http_module._token_store.put_tenant(
        "111", {"library_id": "111", "library_type": "user", "api_key": "new-key"}
    )
    second = http_module.get_service()
    assert second.api_key == "new-key"


def test_get_service_raises_without_authenticated_tenant(
    http_module, fake_zotero_instances, monkeypatch
):
    monkeypatch.setattr(http_module, "get_access_token", lambda: None)
    with pytest.raises(RuntimeError):
        http_module.get_service()


def test_get_service_raises_for_unknown_tenant(
    http_module, fake_zotero_instances, monkeypatch
):
    _set_current_subject(http_module, monkeypatch, "does-not-exist")
    with pytest.raises(RuntimeError):
        http_module.get_service()
