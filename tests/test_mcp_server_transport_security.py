"""Tests app/mcp_server.py's _http_transport_security wiring: DNS-rebinding
protection (Host/Origin header validation, mcp.server.transport_security)
is actually enforced against the streamable-http /mcp endpoint when built
the same way main() builds it -- not just constructed and left unused. The
mcp SDK ships this middleware but leaves it disabled unless a deployment
explicitly opts in (see _http_transport_security's docstring in
app/mcp_server.py), which this server previously wasn't doing.

Requests need a valid bearer token to get past the OAuth resource-server
middleware and actually reach the transport-security check underneath it
(a bare unauthenticated request 401s before ever getting there, regardless
of Host/Origin -- confirmed empirically, not just from reading the SDK),
so these tests run the same register -> authorize -> login -> token dance
as test_mcp_server_oauth.py rather than testing transport security in
isolation.
"""

from __future__ import annotations

import base64
import hashlib
import importlib
import secrets
import sys
from urllib.parse import parse_qs, urlparse

import pytest
from cryptography.fernet import Fernet
from starlette.testclient import TestClient

from app.mcp_server import _http_transport_security


class _FakeZotero:
    """Stands in for pyzotero.zotero.Zotero so /login's live credential
    check succeeds for "valid-key" without a real network call -- see
    test_mcp_server_oauth.py's identical fixture."""

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
    monkeypatch.setenv("PORT", "8298")
    monkeypatch.delenv("ZOTERO_LIBRARY_ID", raising=False)
    monkeypatch.delenv("ZOTERO_API_KEY", raising=False)
    monkeypatch.setenv("MCP_TOKEN_STORE_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("MCP_PUBLIC_URL", "https://example.test")
    monkeypatch.setenv("MCP_DATA_DIR", str(tmp_path))

    sys.modules.pop("app.mcp_server", None)
    module = importlib.import_module("app.mcp_server")

    import app.oauth_provider as oauth_provider_mod

    monkeypatch.setattr(oauth_provider_mod.zotero, "Zotero", _FakeZotero)

    yield module
    sys.modules.pop("app.mcp_server", None)


@pytest.fixture
def secured_client(http_mcp_server):
    app = http_mcp_server.mcp.streamable_http_app(
        transport_security=_http_transport_security(
            http_mcp_server._http_settings.public_url
        )
    )
    with TestClient(app, base_url="https://example.test") as test_client:
        yield test_client


def _code_verifier_and_challenge() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(32)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return verifier, challenge


def _bearer_token(client: TestClient) -> str:
    client_info = client.post(
        "/register",
        json={
            "redirect_uris": ["https://claude.example/callback"],
            "token_endpoint_auth_method": "none",
        },
    ).json()

    verifier, challenge = _code_verifier_and_challenge()
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
    code = parse_qs(urlparse(login_resp.headers["location"]).query)["code"][0]

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
    return token_resp.json()["access_token"]


def test_http_transport_security_settings():
    settings = _http_transport_security("https://example.test")
    assert settings.enable_dns_rebinding_protection is True
    assert settings.allowed_hosts == ["example.test"]
    assert settings.allowed_origins == ["https://example.test"]


def test_wrong_host_rejected(secured_client):
    token = _bearer_token(secured_client)
    resp = secured_client.post(
        "http://evil.test/mcp",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        content="{}",
    )
    assert resp.status_code == 421


def test_wrong_origin_rejected(secured_client):
    token = _bearer_token(secured_client)
    resp = secured_client.post(
        "/mcp",
        headers={
            "Content-Type": "application/json",
            "Origin": "https://evil.test",
            "Authorization": f"Bearer {token}",
        },
        content="{}",
    )
    assert resp.status_code == 403


def test_matching_host_and_origin_not_blocked_by_transport_security(secured_client):
    token = _bearer_token(secured_client)
    resp = secured_client.post(
        "/mcp",
        headers={
            "Content-Type": "application/json",
            "Origin": "https://example.test",
            "Authorization": f"Bearer {token}",
        },
        content="{}",
    )
    # Past the transport-security layer entirely -- whatever status comes
    # next (bad Accept header, malformed body, ...) is a different concern.
    assert resp.status_code not in (401, 421, 403)


def test_absent_origin_not_blocked_by_transport_security(secured_client):
    # A same-origin/non-browser client sending no Origin header at all is
    # valid per TransportSecurityMiddleware._validate_origin -- most real
    # MCP client traffic (server-to-server) falls in this bucket.
    token = _bearer_token(secured_client)
    resp = secured_client.post(
        "/mcp",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        content="{}",
    )
    assert resp.status_code not in (401, 421, 403)
