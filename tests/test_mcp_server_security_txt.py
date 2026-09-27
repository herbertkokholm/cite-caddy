"""Tests app/mcp_server.py's /.well-known/security.txt route (RFC 9116):
MCPBundles' publish flow verifies server ownership by emailing a code to
the Contact address found here. Opt-in via MCP_SECURITY_CONTACT, so a
self-hosted deployment that doesn't set it gets a 404 rather than this
repo's maintainer's address.
"""

from __future__ import annotations

import importlib
import sys
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.fernet import Fernet
from starlette.testclient import TestClient


def _client(tmp_path, monkeypatch, contact: str | None):
    monkeypatch.setenv("PORT", "8299")
    monkeypatch.delenv("ZOTERO_LIBRARY_ID", raising=False)
    monkeypatch.delenv("ZOTERO_API_KEY", raising=False)
    monkeypatch.setenv("MCP_TOKEN_STORE_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("MCP_PUBLIC_URL", "https://example.test")
    monkeypatch.setenv("MCP_DATA_DIR", str(tmp_path))
    # Empty rather than unset for the "no contact" case: load_dotenv() only
    # fills vars missing from os.environ, so a developer's local .env could
    # otherwise re-set it.
    monkeypatch.setenv("MCP_SECURITY_CONTACT", contact or "")

    sys.modules.pop("app.mcp_server", None)
    module = importlib.import_module("app.mcp_server")
    return TestClient(module.mcp.streamable_http_app())


@pytest.fixture(autouse=True)
def _reset_module():
    yield
    sys.modules.pop("app.mcp_server", None)


def _fields(body: str) -> dict[str, str]:
    return dict(line.split(": ", 1) for line in body.strip().splitlines())


def test_security_txt_404_when_contact_unset(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch, None) as client:
        assert client.get("/.well-known/security.txt").status_code == 404


def test_security_txt_bare_email_gets_mailto(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch, "sec@example.test") as client:
        resp = client.get("/.well-known/security.txt")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    fields = _fields(resp.text)
    assert fields["Contact"] == "mailto:sec@example.test"
    assert fields["Canonical"] == "https://example.test/.well-known/security.txt"
    assert fields["Policy"].endswith("/SECURITY.md")


def test_security_txt_uri_contact_passed_through(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch, "https://example.test/report") as client:
        fields = _fields(client.get("/.well-known/security.txt").text)
    assert fields["Contact"] == "https://example.test/report"


def test_security_txt_expires_is_future_and_under_a_year_and_a_day(
    tmp_path, monkeypatch
):
    with _client(tmp_path, monkeypatch, "sec@example.test") as client:
        fields = _fields(client.get("/.well-known/security.txt").text)

    expires = datetime.strptime(fields["Expires"], "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )
    now = datetime.now(timezone.utc)
    assert now < expires <= now + timedelta(days=366)


def test_security_txt_is_unauthenticated(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch, "sec@example.test") as client:
        assert client.get("/.well-known/security.txt").status_code != 401
