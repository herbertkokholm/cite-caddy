"""RFC 8707 resource binding for bearer tokens -- the basis for
app/mcp_server.py's AuthSettings.validate_token_resource=True.

With validate_token_resource=True the mcp SDK's BearerAuthBackend rejects
every token whose AccessToken.resource isn't resource_server_url, *including
tokens with no resource at all*. So CiteCaddyOAuthProvider must bind every
token it issues -- and every legacy token it loads -- to that resource, or
users get locked out. These tests pin down both halves of that.
"""

from __future__ import annotations

import time
from urllib.parse import parse_qs, urlparse

import pytest
from cryptography.fernet import Fernet
from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
)
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyHttpUrl
from starlette.requests import HTTPConnection

import app.oauth_provider as oauth_provider_mod
from app.oauth_provider import CiteCaddyOAuthProvider
from app.oauth_store import TokenStore

RESOURCE_URL = "https://citecaddy.example/mcp"


class _StaticVerifier:
    def __init__(self, token: AccessToken) -> None:
        self._token = token

    async def verify_token(self, token: str) -> AccessToken | None:
        return self._token


def _conn() -> HTTPConnection:
    return HTTPConnection(
        {
            "type": "http",
            "headers": [(b"authorization", b"Bearer opaque-token")],
        }
    )


def _token(resource: str | None) -> AccessToken:
    return AccessToken(
        token="opaque-token",
        client_id="client-1",
        scopes=[],
        expires_at=int(time.time()) + 3600,
        resource=resource,
        subject="123",
    )


@pytest.mark.parametrize(
    ("resource", "accepted"),
    [
        (RESOURCE_URL, True),
        (RESOURCE_URL + "/", True),  # trailing slash is tolerated
        ("https://other.example/mcp", False),
        (None, False),  # no resource => rejected: this is the lockout risk
    ],
)
async def test_bearer_backend_with_resource_validation(resource, accepted):
    backend = BearerAuthBackend(
        _StaticVerifier(_token(resource)),
        resource_server_url=AnyHttpUrl(RESOURCE_URL),
    )
    result = await backend.authenticate(_conn())
    assert (result is not None) is accepted


@pytest.mark.parametrize("resource", [RESOURCE_URL, "https://other.example/mcp", None])
async def test_bearer_backend_without_resource_validation_accepts_all(resource):
    # validate_token_resource=False => the SDK passes resource_server_url=None.
    backend = BearerAuthBackend(_StaticVerifier(_token(resource)))
    assert await backend.authenticate(_conn()) is not None


# ---- provider: every issued/loaded token is bound to RESOURCE_URL --------


class _FakeZotero:
    def __init__(self, library_id, library_type, api_key) -> None:
        self.api_key = api_key

    def key_info(self):
        return {"userID": 1, "access": {}}


@pytest.fixture
def store(tmp_path, monkeypatch) -> TokenStore:
    monkeypatch.setattr(oauth_provider_mod.zotero, "Zotero", _FakeZotero)
    return TokenStore(
        str(tmp_path / "store.json"), fernet_key=Fernet.generate_key().decode()
    )


@pytest.fixture
def provider(store) -> CiteCaddyOAuthProvider:
    return CiteCaddyOAuthProvider(
        store, issuer_url="https://citecaddy.example", resource_url=RESOURCE_URL
    )


@pytest.fixture
def client_info() -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id="client-1",
        redirect_uris=["https://claude.ai/api/mcp/auth_callback"],
    )


def _params(resource: str | None) -> AuthorizationParams:
    return AuthorizationParams(
        state="xyz",
        scopes=None,
        code_challenge="challenge123",
        redirect_uri="https://claude.ai/api/mcp/auth_callback",
        redirect_uri_provided_explicitly=True,
        resource=resource,
    )


async def _issue(provider, client_info, resource):
    url = await provider.authorize(client_info, _params(resource))
    login_id = url.split("login_id=")[1]
    redirect_url = await provider.complete_login(login_id, "123", "user", "valid-key")
    code = parse_qs(urlparse(redirect_url).query)["code"][0]
    auth_code = await provider.load_authorization_code(client_info, code)
    assert auth_code.resource == RESOURCE_URL
    return await provider.exchange_authorization_code(client_info, auth_code)


@pytest.mark.parametrize(
    "resource",
    [
        RESOURCE_URL,
        RESOURCE_URL + "/",
        "HTTPS://CiteCaddy.example:443/mcp",
        None,  # client omitted `resource`
    ],
)
async def test_issued_and_refreshed_tokens_carry_resource(
    provider, client_info, resource
):
    tokens = await _issue(provider, client_info, resource)
    access = await provider.load_access_token(tokens.access_token)
    refresh = await provider.load_refresh_token(client_info, tokens.refresh_token)
    assert access.resource == RESOURCE_URL
    assert refresh.resource == RESOURCE_URL

    refreshed = await provider.exchange_refresh_token(client_info, refresh, [])
    new_access = await provider.load_access_token(refreshed.access_token)
    new_refresh = await provider.load_refresh_token(
        client_info, refreshed.refresh_token
    )
    assert new_access.resource == RESOURCE_URL
    assert new_refresh.resource == RESOURCE_URL


@pytest.mark.parametrize(
    "resource", ["https://other.example/mcp", "https://citecaddy.example/", "nope"]
)
async def test_authorize_rejects_foreign_resource(provider, client_info, resource):
    with pytest.raises(AuthorizeError) as exc:
        await provider.authorize(client_info, _params(resource))
    assert exc.value.error == "invalid_target"


async def test_legacy_tokens_without_resource_are_backfilled(
    provider, store, client_info
):
    # Shape of tokens persisted by <= 2.3.12, which never set `resource`.
    common = {"client_id": "client-1", "scopes": [], "subject": "123"}
    store.put(
        "access_tokens",
        "old-access",
        AccessToken(
            token="old-access", expires_at=int(time.time()) + 600, **common
        ).model_dump(mode="json"),
    )
    store.put(
        "refresh_tokens",
        "old-refresh",
        RefreshToken(token="old-refresh", expires_at=None, **common).model_dump(
            mode="json"
        ),
    )

    access = await provider.load_access_token("old-access")
    assert access.resource == RESOURCE_URL
    backend = BearerAuthBackend(
        _StaticVerifier(access), resource_server_url=AnyHttpUrl(RESOURCE_URL)
    )
    assert await backend.authenticate(_conn()) is not None

    refresh = await provider.load_refresh_token(client_info, "old-refresh")
    assert refresh.resource == RESOURCE_URL
    tokens = await provider.exchange_refresh_token(client_info, refresh, [])
    assert (await provider.load_access_token(tokens.access_token)).resource == (
        RESOURCE_URL
    )


async def test_resource_url_defaults_to_issuer_mcp_endpoint(store):
    provider = CiteCaddyOAuthProvider(store, issuer_url="https://citecaddy.example/")
    assert provider._resource_url == RESOURCE_URL
