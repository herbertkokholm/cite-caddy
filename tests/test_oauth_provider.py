import time
from urllib.parse import parse_qs, urlparse

import pytest
from mcp.server.auth.provider import AccessToken, AuthorizationParams, RefreshToken
from mcp.shared.auth import InvalidRedirectUriError, OAuthClientInformationFull

from app.oauth_provider import (
    InvalidCredentialsError,
    LoginSessionExpiredError,
    ZoteroMCPOAuthProvider,
)
from app.oauth_store import TokenStore


@pytest.fixture
def provider(tmp_path) -> ZoteroMCPOAuthProvider:
    store = TokenStore(str(tmp_path / "store.json"))
    return ZoteroMCPOAuthProvider(store, username="thomas", password="s3cret")


@pytest.fixture
def client_info() -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id="client-1",
        redirect_uris=["https://claude.ai/api/mcp/auth_callback"],
    )


def auth_params(**overrides) -> AuthorizationParams:
    defaults = {
        "state": "xyz",
        "scopes": None,
        "code_challenge": "challenge123",
        "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
        "redirect_uri_provided_explicitly": True,
        "resource": None,
    }
    defaults.update(overrides)
    return AuthorizationParams(**defaults)


async def test_register_and_get_client(provider, client_info):
    await provider.register_client(client_info)
    loaded = await provider.get_client("client-1")
    assert loaded is not None
    assert loaded.client_id == "client-1"


async def test_get_unknown_client_is_auto_provisioned(provider):
    # Some MCP clients (observed: Claude Desktop/claude.ai) skip dynamic
    # client registration and send /authorize a self-chosen client_id with
    # no prior POST /register -- auto-provisioning instead of rejecting is
    # what makes that still work; see _FlexibleClientInformation's docstring.
    client = await provider.get_client("some-client-id")
    assert client is not None
    assert client.client_id == "some-client-id"

    # Persisted -- a second lookup returns the same (now-registered) client.
    again = await provider.get_client("some-client-id")
    assert again is not None
    assert again.client_id == "some-client-id"


async def test_auto_provisioned_client_accepts_any_redirect_uri(provider):
    client = await provider.get_client("some-client-id")
    # No redirect_uris were registered for it, so any explicitly-provided
    # redirect_uri is accepted rather than rejected.
    assert client.validate_redirect_uri("https://anything.example/callback") == (
        "https://anything.example/callback"
    )


async def test_registered_client_still_rejects_unregistered_redirect_uri(
    provider, client_info
):
    await provider.register_client(client_info)
    loaded = await provider.get_client("client-1")
    with pytest.raises(InvalidRedirectUriError):
        loaded.validate_redirect_uri("https://not-registered.example/callback")


async def test_authorize_returns_login_url_with_pending_state(provider, client_info):
    url = await provider.authorize(client_info, auth_params())
    assert url.startswith("/login?login_id=")

    login_id = url.split("login_id=")[1]
    pending = provider.get_pending(login_id)
    assert pending.client_id == "client-1"


async def test_get_pending_raises_for_unknown_login_id(provider):
    with pytest.raises(LoginSessionExpiredError):
        provider.get_pending("does-not-exist")


async def test_complete_login_wrong_password_keeps_pending_for_retry(
    provider, client_info
):
    url = await provider.authorize(client_info, auth_params())
    login_id = url.split("login_id=")[1]

    with pytest.raises(InvalidCredentialsError):
        await provider.complete_login(login_id, "thomas", "wrong")

    # Pending authorization must still be there -- a typo shouldn't burn the flow.
    assert provider.get_pending(login_id) is not None


async def test_complete_login_success_redirects_with_code_and_state(
    provider, client_info
):
    url = await provider.authorize(client_info, auth_params(state="my-state"))
    login_id = url.split("login_id=")[1]

    redirect_url = await provider.complete_login(login_id, "thomas", "s3cret")

    parsed = urlparse(redirect_url)
    assert parsed.netloc == "claude.ai"
    qs = parse_qs(parsed.query)
    assert qs["state"] == ["my-state"]
    assert "code" in qs

    # Login session is consumed -- can't be replayed.
    with pytest.raises(LoginSessionExpiredError):
        provider.get_pending(login_id)


async def test_authorization_code_can_be_exchanged_once(provider, client_info):
    url = await provider.authorize(client_info, auth_params())
    login_id = url.split("login_id=")[1]
    redirect_url = await provider.complete_login(login_id, "thomas", "s3cret")
    code = parse_qs(urlparse(redirect_url).query)["code"][0]

    auth_code = await provider.load_authorization_code(client_info, code)
    assert auth_code is not None
    assert auth_code.client_id == "client-1"

    tokens = await provider.exchange_authorization_code(client_info, auth_code)
    assert tokens.access_token
    assert tokens.refresh_token
    assert tokens.token_type == "Bearer"

    # Code is single-use.
    assert await provider.load_authorization_code(client_info, code) is None


async def test_load_access_token_roundtrip(provider):
    access_token = AccessToken(
        token="tok1",
        client_id="client-1",
        scopes=[],
        expires_at=int(time.time()) + 3600,
    )
    provider._store.put("access_tokens", "tok1", access_token.model_dump(mode="json"))

    loaded = await provider.load_access_token("tok1")
    assert loaded is not None
    assert loaded.client_id == "client-1"


async def test_load_access_token_expired_returns_none(provider):
    access_token = AccessToken(
        token="tok-old",
        client_id="client-1",
        scopes=[],
        expires_at=int(time.time()) - 10,
    )
    provider._store.put(
        "access_tokens", "tok-old", access_token.model_dump(mode="json")
    )

    assert await provider.load_access_token("tok-old") is None


async def test_load_access_token_unknown_returns_none(provider):
    assert await provider.load_access_token("never-issued") is None


async def test_exchange_refresh_token_rotates_tokens(provider, client_info):
    refresh_token = RefreshToken(
        token="r1", client_id="client-1", scopes=["a"], expires_at=None
    )
    provider._store.put("refresh_tokens", "r1", refresh_token.model_dump(mode="json"))

    loaded = await provider.load_refresh_token(client_info, "r1")
    assert loaded is not None

    tokens = await provider.exchange_refresh_token(client_info, loaded, ["a"])
    assert tokens.access_token
    assert tokens.refresh_token != "r1"

    # Old refresh token is consumed.
    assert await provider.load_refresh_token(client_info, "r1") is None


async def test_revoke_token_removes_access_token(provider):
    access_token = AccessToken(
        token="tok2", client_id="client-1", scopes=[], expires_at=None
    )
    provider._store.put("access_tokens", "tok2", access_token.model_dump(mode="json"))

    await provider.revoke_token(access_token)

    assert await provider.load_access_token("tok2") is None
