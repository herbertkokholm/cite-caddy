"""OAuth 2.1 authorization server for this MCP server, implementing the
`mcp` SDK's `OAuthAuthorizationServerProvider` protocol.

Why this exists: the MCP connector UI in Claude Desktop/claude.ai is
OAuth-first -- given a remote server URL, it always tries the OAuth
discovery + authorization-code dance, and treats a plain 401 (e.g. from a
reverse proxy doing HTTP Basic Auth in front of the server) as "this
server needs OAuth", failing with a 404 when it then hits a nonexistent
/authorize endpoint. Implementing the real thing is what makes "Add
custom connector" work.

This server is multi-tenant self-service: anyone can bring their own
Zotero library. "authorize" doesn't delegate to a third-party identity
provider -- it's a small first-party login form (see the /login routes in
app/mcp_server.py) asking for a Zotero Library ID, Library Type, and API
Key. Completing that form IS onboarding: the first successful validation
of the API key (see complete_login below) both gates access and persists
the tenant's credentials (app/oauth_store.py's TokenStore "tenants"
collection) for that library_id, no separate signup step. Any MCP client
can dynamically register itself (RFC 7591, POST /register) and request
authorization, but completing the login form is what actually gates
access. A proxy-level Basic Auth gate in front of the whole server would
instead block /register, /token, and the OAuth discovery endpoints
themselves, which must be reachable without pre-authentication for the
flow to work at all.

Access tokens are opaque random strings (not JWTs) looked up in
app/oauth_store.py's TokenStore -- fine for a single-server deployment with
no need to verify tokens anywhere else. Each issued AccessToken/
RefreshToken carries `subject=library_id` (see _issue_tokens), so
app/mcp_server.py's get_service() can look up which tenant's Zotero
credentials to use for a given bearer token.
"""

from __future__ import annotations

import secrets
import time

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    construct_redirect_uri,
)
from mcp.shared.auth import (
    InvalidRedirectUriError,
    OAuthClientInformationFull,
    OAuthToken,
)
from pyzotero import zotero

from app.oauth_store import TokenStore

AUTH_CODE_TTL_SECONDS = 300  # 5 minutes to complete the login form
ACCESS_TOKEN_TTL_SECONDS = 3600  # 1 hour; refresh tokens don't expire


class _FlexibleClientInformation(OAuthClientInformationFull):
    """An OAuthClientInformationFull that accepts any redirect_uri when none
    were registered (redirect_uris=None), instead of always rejecting.

    Needed because some MCP clients (observed: Claude Desktop/claude.ai)
    skip dynamic client registration (RFC 7591, POST /register) entirely
    and just send /authorize a self-chosen client_id with no prior
    registration -- "Client ID '...' not found" otherwise. This class is
    only lenient when redirect_uris is unset (i.e. for clients
    auto-provisioned by get_client below); a client that DID register
    through /register keeps normal strict validation against the
    redirect_uris it registered. The trade-off is deliberate: the actual
    access gate is the /login form's live Zotero credential check (see
    complete_login below), not client_id or redirect_uri matching -- each
    tenant's own Zotero API key is what protects their library, regardless
    of which client_id requested the authorization.
    """

    def validate_redirect_uri(self, redirect_uri):
        if self.redirect_uris:
            return super().validate_redirect_uri(redirect_uri)
        if redirect_uri is not None:
            return redirect_uri
        raise InvalidRedirectUriError("redirect_uri must be specified")


class PendingAuthorization:
    """An in-flight /authorize request waiting on the /login form. Kept
    in-memory only (not persisted): if the container restarts mid-login,
    the user just retries -- not worth persisting a half-finished flow."""

    def __init__(self, client_id: str, params: AuthorizationParams) -> None:
        self.client_id = client_id
        self.params = params
        self.created_at = time.time()


class InvalidCredentialsError(Exception):
    pass


class LoginSessionExpiredError(Exception):
    pass


class ZoteroMCPOAuthProvider(OAuthAuthorizationServerProvider):
    def __init__(self, store: TokenStore) -> None:
        self._store = store
        self._pending: dict[str, PendingAuthorization] = {}

    # ---- dynamic client registration ----------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        data = self._store.get("clients", client_id)
        if data:
            return _FlexibleClientInformation.model_validate(data)

        # Auto-provision: see _FlexibleClientInformation's docstring for why
        # an unregistered client_id is accepted here rather than rejected.
        client_info = _FlexibleClientInformation(
            client_id=client_id, redirect_uris=None, token_endpoint_auth_method="none"
        )
        self._store.put("clients", client_id, client_info.model_dump(mode="json"))
        return client_info

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self._store.put(
            "clients", client_info.client_id, client_info.model_dump(mode="json")
        )

    # ---- authorize -> first-party login form ---------------------------

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        login_id = secrets.token_urlsafe(24)
        self._pending[login_id] = PendingAuthorization(client.client_id, params)
        return f"/login?login_id={login_id}"

    def get_pending(self, login_id: str) -> PendingAuthorization:
        pending = self._pending.get(login_id)
        if pending is None or time.time() - pending.created_at > AUTH_CODE_TTL_SECONDS:
            self._pending.pop(login_id, None)
            raise LoginSessionExpiredError(
                "Login link expired -- go back to your MCP client and try connecting again."
            )
        return pending

    async def complete_login(
        self, login_id: str, library_id: str, library_type: str, api_key: str
    ) -> str:
        """Called by the /login POST handler. Validates the submitted
        Zotero credentials by actually calling the Zotero API with them,
        persists them as a tenant (upsert -- a returning user re-entering
        the same or a rotated key just overwrites the stored one), and
        returns the URL to redirect the browser to (back to the MCP
        client's redirect_uri, with a fresh authorization code). Raises
        InvalidCredentialsError/LoginSessionExpiredError on failure.

        This is the entire onboarding flow: there's no separate signup
        step, no admin approval -- a Zotero API key that Zotero itself
        accepts is sufficient to self-serve onto this server.
        """
        pending = self.get_pending(login_id)
        library_id = library_id.strip()

        if library_type not in ("user", "group"):
            raise InvalidCredentialsError('Library Type must be "user" or "group".')

        try:
            zot = zotero.Zotero(library_id, library_type, api_key)
            zot.key_info()
        except Exception as exc:
            raise InvalidCredentialsError(
                "Could not verify that Zotero Library ID/API Key -- check "
                "them and try again."
            ) from exc

        self._store.put_tenant(
            library_id,
            {
                "library_id": library_id,
                "library_type": library_type,
                "api_key": api_key,
            },
        )

        del self._pending[login_id]
        code = secrets.token_urlsafe(32)
        auth_code = AuthorizationCode(
            code=code,
            scopes=pending.params.scopes or [],
            expires_at=time.time() + AUTH_CODE_TTL_SECONDS,
            client_id=pending.client_id,
            code_challenge=pending.params.code_challenge,
            redirect_uri=pending.params.redirect_uri,
            redirect_uri_provided_explicitly=pending.params.redirect_uri_provided_explicitly,
            resource=pending.params.resource,
            subject=library_id,
        )
        self._store.put("auth_codes", code, auth_code.model_dump(mode="json"))

        return construct_redirect_uri(
            str(pending.params.redirect_uri),
            code=code,
            state=pending.params.state,
        )

    # ---- authorization code exchange -----------------------------------

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        data = self._store.get("auth_codes", authorization_code)
        return AuthorizationCode.model_validate(data) if data else None

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        self._store.delete("auth_codes", authorization_code.code)
        return self._issue_tokens(
            client.client_id, authorization_code.scopes, authorization_code.subject
        )

    # ---- refresh tokens --------------------------------------------------

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        data = self._store.get("refresh_tokens", refresh_token)
        return RefreshToken.model_validate(data) if data else None

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        self._store.delete("refresh_tokens", refresh_token.token)
        return self._issue_tokens(
            client.client_id, scopes or refresh_token.scopes, refresh_token.subject
        )

    def _issue_tokens(
        self, client_id: str, scopes: list[str], subject: str | None
    ) -> OAuthToken:
        access_token = secrets.token_urlsafe(32)
        refresh_token = secrets.token_urlsafe(32)
        expires_at = int(time.time()) + ACCESS_TOKEN_TTL_SECONDS

        self._store.put(
            "access_tokens",
            access_token,
            AccessToken(
                token=access_token,
                client_id=client_id,
                scopes=scopes,
                expires_at=expires_at,
                subject=subject,
            ).model_dump(mode="json"),
        )
        self._store.put(
            "refresh_tokens",
            refresh_token,
            RefreshToken(
                token=refresh_token,
                client_id=client_id,
                scopes=scopes,
                expires_at=None,
                subject=subject,
            ).model_dump(mode="json"),
        )
        return OAuthToken(
            access_token=access_token,
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_TTL_SECONDS,
            refresh_token=refresh_token,
            scope=" ".join(scopes) if scopes else None,
        )

    # ---- bearer token verification (used on every /mcp request) ---------

    async def load_access_token(self, token: str) -> AccessToken | None:
        data = self._store.get("access_tokens", token)
        if not data:
            return None
        access_token = AccessToken.model_validate(data)
        if (
            access_token.expires_at is not None
            and access_token.expires_at < time.time()
        ):
            self._store.delete("access_tokens", token)
            return None
        return access_token

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        kind = "access_tokens" if isinstance(token, AccessToken) else "refresh_tokens"
        self._store.delete(kind, token.token)
