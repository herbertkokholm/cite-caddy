"""Integration test for app/mcp_server.py's HTTP-mode wiring: the real
FastMCP app, the real mcp SDK OAuth routes (/register, /authorize,
/token), and this project's own /login routes and ZoteroMCPOAuthProvider,
all together -- confirming the plumbing is correct, not re-testing the SDK
or the provider's internals (covered in test_oauth_provider.py).

app/mcp_server.py builds its FastMCP instance and OAuth provider at import
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
from starlette.testclient import TestClient


def _code_verifier_and_challenge() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(32)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return verifier, challenge


@pytest.fixture
def http_mcp_server(tmp_path, monkeypatch):
    monkeypatch.setenv("PORT", "8199")
    monkeypatch.setenv("ZOTERO_LIBRARY_ID", "123")
    monkeypatch.setenv("ZOTERO_API_KEY", "fake-key")
    monkeypatch.setenv("MCP_AUTH_USERNAME", "thomas")
    monkeypatch.setenv("MCP_AUTH_PASSWORD", "s3cret")
    monkeypatch.setenv("MCP_PUBLIC_URL", "https://example.test")
    monkeypatch.setenv("MCP_DATA_DIR", str(tmp_path))

    sys.modules.pop("app.mcp_server", None)
    module = importlib.import_module("app.mcp_server")
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


def test_login_wrong_password_shows_error(client):
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
        data={"login_id": login_id, "username": "thomas", "password": "wrong"},
    )
    assert resp.status_code == 401
    assert "Incorrect" in resp.text


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
        data={"login_id": login_id, "username": "thomas", "password": "s3cret"},
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
    # Not 401 -- the token authenticated; 406 is FastMCP's own response to a
    # plain GET without the streamable-http Accept headers, expected here.
    assert mcp_resp.status_code != 401
