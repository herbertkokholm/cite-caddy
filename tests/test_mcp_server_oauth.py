"""Integration test for app/mcp_server.py's HTTP-mode wiring: the real
MCPServer app, the real mcp SDK OAuth routes (/register, /authorize,
/token), and this project's own /login routes and CiteCaddyOAuthProvider,
all together -- confirming the plumbing is correct, not re-testing the SDK
or the provider's internals (covered in test_oauth_provider.py).

app/mcp_server.py builds its MCPServer instance and OAuth provider at import
time from env vars, so this module is imported fresh (via importlib, after
clearing sys.modules) under test-specific env vars -- other test modules
import it without $PORT set, for the stdio-mode tool tests.
"""

from __future__ import annotations

import base64
import hashlib
import importlib
import secrets
import sys

import pytest
from cryptography.fernet import Fernet
from starlette.testclient import TestClient


def _code_verifier_and_challenge() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(32)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return verifier, challenge


class _FakeZotero:
    """Stands in for pyzotero.zotero.Zotero for these HTTP-level tests --
    key_info() succeeds for "valid-key" and raises for anything else, so
    /login's live Zotero credential check can be exercised without a real
    network call. See app/oauth_provider.py's complete_login."""

    def __init__(self, library_id, library_type, api_key) -> None:
        self.library_id = library_id
        self.library_type = library_type
        self.api_key = api_key

    def key_info(self):
        if self.api_key != "valid-key":
            raise RuntimeError("invalid key")
        return {"userID": 1, "access": {}}


@pytest.fixture
def http_mcp_server(tmp_path, monkeypatch):
    monkeypatch.setenv("PORT", "8199")
    monkeypatch.delenv("ZOTERO_LIBRARY_ID", raising=False)
    monkeypatch.delenv("ZOTERO_API_KEY", raising=False)
    monkeypatch.setenv("MCP_TOKEN_STORE_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("MCP_PUBLIC_URL", "https://example.test")
    monkeypatch.setenv("MCP_DATA_DIR", str(tmp_path))

    sys.modules.pop("app.mcp_server", None)
    module = importlib.import_module("app.mcp_server")
    # app.metrics is a long-lived module (re-importing app.mcp_server above
    # reuses the same one -- see its reset() docstring), so tests get a
    # clean slate explicitly instead of relying on a fresh import.
    module.metrics.reset()

    import app.oauth_provider as oauth_provider_mod

    monkeypatch.setattr(oauth_provider_mod.zotero, "Zotero", _FakeZotero)

    yield module
    sys.modules.pop("app.mcp_server", None)


@pytest.fixture
def client(http_mcp_server) -> TestClient:
    # The streamable-http session manager needs its lifespan (task group)
    # started, which only happens when TestClient is used as a context
    # manager -- a bare TestClient(app) skips startup/shutdown entirely.
    with TestClient(http_mcp_server.mcp.streamable_http_app()) as test_client:
        yield test_client


def _register_client(client: TestClient) -> dict:
    resp = client.post(
        "/register",
        json={
            "redirect_uris": ["https://claude.example/callback"],
            "token_endpoint_auth_method": "none",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _login_page_response(client: TestClient):
    client_info = _register_client(client)
    _, challenge = _code_verifier_and_challenge()
    authorize_resp = client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_info["client_id"],
            "redirect_uri": "https://claude.example/callback",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "xyz",
        },
        follow_redirects=False,
    )
    return client.get(authorize_resp.headers["location"])


def test_healthz_is_public(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200


def test_mcp_endpoint_requires_bearer_token(client):
    resp = client.get("/mcp")
    assert resp.status_code == 401


def test_dynamic_client_registration(client):
    info = _register_client(client)
    assert info["client_id"]
    assert info["redirect_uris"] == ["https://claude.example/callback"]


def test_authorize_redirects_to_login_form(client):
    client_info = _register_client(client)
    _, challenge = _code_verifier_and_challenge()

    resp = client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_info["client_id"],
            "redirect_uri": "https://claude.example/callback",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "xyz",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert resp.headers["location"].startswith("/login?login_id=")

    login_page = client.get(resp.headers["location"])
    assert login_page.status_code == 200
    assert "Sign in" in login_page.text


def test_authorize_without_prior_registration_still_works(client):
    """Reproduces the bug seen with Claude Desktop/claude.ai: it hit
    /authorize with a self-chosen client_id and no prior POST /register,
    getting "Client ID '...' not found". The server now auto-provisions
    unregistered client_ids instead of rejecting them (see
    app.oauth_provider._FlexibleClientInformation)."""
    _, challenge = _code_verifier_and_challenge()

    resp = client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": "some-unregistered-client-id",
            "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "xyz",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert resp.headers["location"].startswith("/login?login_id=")


def test_login_invalid_zotero_key_shows_error(client):
    client_info = _register_client(client)
    _, challenge = _code_verifier_and_challenge()
    authorize_resp = client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_info["client_id"],
            "redirect_uri": "https://claude.example/callback",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
        follow_redirects=False,
    )
    login_id = authorize_resp.headers["location"].split("login_id=")[1]

    resp = client.post(
        "/login",
        data={
            "login_id": login_id,
            "library_id": "123",
            "library_type": "user",
            "api_key": "wrong-key",
        },
    )
    assert resp.status_code == 401
    assert "Could not verify" in resp.text


def test_full_oauth_flow_yields_working_bearer_token(client):
    client_info = _register_client(client)
    verifier, challenge = _code_verifier_and_challenge()

    authorize_resp = client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_info["client_id"],
            "redirect_uri": "https://claude.example/callback",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "abc123",
        },
        follow_redirects=False,
    )
    login_id = authorize_resp.headers["location"].split("login_id=")[1]

    login_resp = client.post(
        "/login",
        data={
            "login_id": login_id,
            "library_id": "123",
            "library_type": "user",
            "api_key": "valid-key",
        },
        follow_redirects=False,
    )
    assert login_resp.status_code == 302
    redirect_location = login_resp.headers["location"]
    assert redirect_location.startswith("https://claude.example/callback")
    assert "state=abc123" in redirect_location

    from urllib.parse import parse_qs, urlparse

    code = parse_qs(urlparse(redirect_location).query)["code"][0]

    token_resp = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://claude.example/callback",
            "client_id": client_info["client_id"],
            "code_verifier": verifier,
        },
    )
    assert token_resp.status_code == 200, token_resp.text
    access_token = token_resp.json()["access_token"]

    mcp_resp = client.get("/mcp", headers={"Authorization": f"Bearer {access_token}"})
    # Not 401 -- the token authenticated; 406 is MCPServer's own response to a
    # plain GET without the streamable-http Accept headers, expected here.
    assert mcp_resp.status_code != 401


# ---- /status ----------------------------------------------------------


def test_status_is_public(client):
    resp = client.get("/status")
    assert resp.status_code == 200


def test_status_baseline_shape_before_any_activity(client):
    body = client.get("/status").json()
    assert body["tenants"] == 0
    assert body["tool_calls"] == {}
    assert body["tool_errors"] == {}
    assert body["uptime_seconds"] >= 0
    assert body["version"]


def test_status_reports_onboarded_tenant_count(http_mcp_server, client):
    http_mcp_server._token_store.put_tenant(
        "123", {"library_id": "123", "library_type": "user", "api_key": "s3cr3t"}
    )

    assert client.get("/status").json()["tenants"] == 1


# ---- tools/call tracking middleware ------------------------------------


def _tool_call_ctx(name: str):
    from mcp.server.context import ServerRequestContext

    return ServerRequestContext(
        session=None,
        lifespan_context={},
        protocol_version="2026-07-28",
        method="tools/call",
        params={"name": name},
    )


def test_track_tool_call_is_registered_on_the_server(http_mcp_server):
    assert http_mcp_server._track_tool_call in http_mcp_server.mcp.middleware


async def test_track_tool_call_counts_success(http_mcp_server):
    async def call_next(ctx):
        return {"ok": True}

    await http_mcp_server._track_tool_call(_tool_call_ctx("search_items"), call_next)

    assert http_mcp_server.metrics.tool_calls["search_items"] == 1
    assert http_mcp_server.metrics.tool_errors["search_items"] == 0


async def test_track_tool_call_counts_error_and_reraises(http_mcp_server):
    async def call_next(ctx):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await http_mcp_server._track_tool_call(_tool_call_ctx("add_tags"), call_next)

    assert http_mcp_server.metrics.tool_calls["add_tags"] == 1
    assert http_mcp_server.metrics.tool_errors["add_tags"] == 1


async def test_track_tool_call_ignores_non_tool_methods(http_mcp_server):
    from mcp.server.context import ServerRequestContext

    seen = []

    async def call_next(ctx):
        seen.append(ctx.method)

    ctx = ServerRequestContext(
        session=None,
        lifespan_context={},
        protocol_version="2026-07-28",
        method="initialize",
        params=None,
    )
    await http_mcp_server._track_tool_call(ctx, call_next)

    assert seen == ["initialize"]
    assert sum(http_mcp_server.metrics.tool_calls.values()) == 0


async def test_status_reflects_tracked_tool_calls(http_mcp_server, client):
    async def call_next(ctx):
        return {"ok": True}

    await http_mcp_server._track_tool_call(_tool_call_ctx("search_items"), call_next)
    await http_mcp_server._track_tool_call(_tool_call_ctx("search_items"), call_next)

    body = client.get("/status").json()
    assert body["tool_calls"] == {"search_items": 2}
    assert body["tool_errors"] == {}


# ---- /status.html -------------------------------------------------------


def test_status_html_is_public(client):
    resp = client.get("/status.html")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


def test_status_html_shows_name_table_and_no_icon_by_default(client):
    body = client.get("/status.html").text
    assert "Cite Caddy" in body
    assert "<table>" in body
    assert "icon.svg" not in body


def test_status_html_shows_placeholder_before_any_activity(client):
    assert "No tool calls yet." in client.get("/status.html").text


def test_status_html_shows_icon_when_website_url_configured(
    http_mcp_server, client, monkeypatch
):
    monkeypatch.setattr(http_mcp_server, "_WEBSITE_URL", "https://example.test/")

    body = client.get("/status.html").text

    assert '<img src="https://example.test/icons/icon.svg"' in body


async def test_status_html_lists_tracked_tool_calls(http_mcp_server, client):
    async def call_next(ctx):
        return {"ok": True}

    await http_mcp_server._track_tool_call(_tool_call_ctx("search_items"), call_next)

    body = client.get("/status.html").text

    assert "search_items" in body
    assert "No tool calls yet." not in body


# ---- login page icon -----------------------------------------------------


def test_login_page_omits_icon_by_default(client):
    assert "icon.svg" not in _login_page_response(client).text


def test_login_page_shows_icon_when_website_url_configured(
    http_mcp_server, client, monkeypatch
):
    monkeypatch.setattr(http_mcp_server, "_WEBSITE_URL", "https://example.test/")

    body = _login_page_response(client).text

    assert '<img src="https://example.test/icons/icon.svg"' in body
