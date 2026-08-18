"""Remote MCP server exposing full read/write access to a Zotero library
(search, add, tag, update, delete, move, attachments, full text, notes,
collection rename/delete, library-wide tag rename/delete, trash view/
move/restore, saved searches, groups, item-type/field/creator-type
schema introspection) -- see README for the key safety note on
delete/move breaking Word-plugin citations.

Transport is chosen by environment: any host setting $PORT means "serve
over streamable-http, bound to 0.0.0.0:$PORT"; without $PORT, this runs
over stdio for local use (`uv run app/mcp_server.py` via an MCP client
config).

Every mutating tool that touches an existing item (update_item, tag tools,
collection-membership tools, delete_item_permanently,
move_item_to_different_library) requires the caller to pass back that
item's current `version` (get from search_items/get_item first). A stale or
wrong version is refused rather than silently overwritten -- see
app/zotero_service.py's module docstring for why that matters here in
particular: a clobbered concurrent edit is normally just an inconvenience,
but on a citation-bearing item it can quietly break a Word document.

Access control: when serving over HTTP, this server is a full OAuth 2.1
authorization server for itself (app/oauth_provider.py), rather than
relying on HTTP Basic Auth in front of it. That's a deliberate choice:
Claude Desktop/claude.ai's "Add custom connector" UI is OAuth-first, and
treats any 401 (which is what Basic Auth in front of the server would
produce) as "this server requires OAuth", then fails when it discovers
there's no real OAuth server behind it. Implementing the real thing is
what makes "Add custom connector" work at all. This server is
multi-tenant and self-service: /authorize doesn't delegate to a third
party -- completing a first-party login form (see /login below) with the
caller's own Zotero Library ID/Type/API Key is what gates access AND
onboards that library as a tenant, in one step; see
app/oauth_provider.py's module docstring for the full flow. Each
authenticated caller's tool calls are routed to their own Zotero library
via get_service() below, keyed off the bearer token's `subject` (the
tenant's library_id).

Env vars (see README's "Configuration" section):
    ZOTERO_LIBRARY_ID      numeric library ID (user or group) -- stdio mode only
    ZOTERO_LIBRARY_TYPE    "user" or "group" (default: user) -- stdio mode only
    ZOTERO_API_KEY         needs write permission -- stdio mode only
    MCP_TOKEN_STORE_KEY    Fernet key encrypting tenants' API keys at rest (HTTP mode only)
    MCP_PUBLIC_URL         public HTTPS URL, e.g. https://your-domain.example
    MCP_DATA_DIR           where OAuth clients/tokens/tenants persist (default: ./.data)
    MCP_WEBSITE_URL        optional; if set, this server's website_url/icons/icon.svg
                            is reported in serverInfo (icons.src is this + "icons/icon.svg")
"""

from __future__ import annotations

import html
import json
import os
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from mcp.server import MCPServer
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import (
    AuthSettings,
    ClientRegistrationOptions,
    RevocationOptions,
)
from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import Icon, Tool, ToolAnnotations
from starlette.requests import Request
from starlette.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)

from app import metrics
from app.config import HttpSettings, Settings, load_dotenv
from app.oauth_provider import (
    CiteCaddyOAuthProvider,
    InvalidCredentialsError,
    LoginSessionExpiredError,
)
from app.oauth_store import TokenStore
from app.zotero_service import ZoteroService

load_dotenv()

_PORT = os.environ.get("PORT")
_INSTRUCTIONS = (
    "Full read/write access to a Zotero library: search, add, tag, update, "
    "delete, and move items; create, rename/move, and delete collections; "
    "list/rename/delete tags library-wide; view/create/delete saved "
    "searches; list groups; look up valid item types/fields/creator "
    "types; list/download/upload item attachments and read their "
    "extracted full text; list/add/edit item notes. Mutating "
    "tools that touch an existing item or collection require its current "
    "`version` (from search_items/get_item/list_collections) and refuse "
    "the write if it's stale -- re-fetch and retry in that case, don't "
    "just resend the same version. delete_item_permanently and "
    "move_item_to_different_library break any Word-document citation "
    "referencing the item's key; prefer "
    "add_to_collection/remove_from_collection for in-library "
    "reorganization, which is safe. delete_collection cascades to any "
    "sub-collections but never deletes the items filed in them. "
    "rename_tag/delete_tag act on every item in the library carrying that "
    "tag, not just one -- use add_tags/remove_tags/set_tags for a single "
    "item's tags. trash_item is a reversible soft delete (undo with "
    "restore_from_trash); delete_item_permanently is not reversible -- "
    "prefer trash_item when a delete might need to be undone. "
    'list_groups\' results (`id`, with target_library_type="group") are '
    "for target_library_id on move_item_to_different_library. "
    "list_item_types/list_item_type_fields/list_item_creator_types "
    "describe what create_item/update_item will actually accept for a "
    "given item_type -- check them instead of guessing when a create/"
    "update call raises a validation error, or before constructing "
    "fields/creators for an unfamiliar item_type. list_creator_fields "
    "lists the name-shape fields (firstName, lastName, name, ...) valid "
    "on a creators entry itself -- not the same as list_item_creator_types, "
    "which lists creatorType roles (author, editor, ...). Attachment file "
    "content travels as base64 (upload_attachment's content_base64, "
    "download_attachment's content_base64 in the result) since this "
    "server runs remotely with no access to the caller's local "
    "filesystem. delete_item_permanently, delete_collection, delete_tag, "
    "delete_saved_search, move_item_to_different_library, and "
    "update_publication_status all accept an optional idempotency_key: "
    "reuse the same key when retrying a call after a lost/ambiguous "
    "response and the original outcome (success or error) is replayed "
    "instead of running against Zotero again -- use a fresh key per "
    "distinct request, not one key for every call. "
    "update_publication_status updates an item's fields (and, uniquely, "
    "its item_type) in place when a preprint is formally published -- "
    "prefer it (or update_item) over delete+recreate. export_bibliography "
    "generates formatted references (HTML bibliography/citation entries in "
    "a given CSL style) or portable export data (CSL-JSON, BibTeX) for a "
    "list of item keys."
)

_WEBSITE_URL = os.environ.get("MCP_WEBSITE_URL")
_ICONS = (
    [
        Icon(
            src=f"{_WEBSITE_URL}icons/icon.svg",
            mimeType="image/svg+xml",
            sizes=["any"],
        )
    ]
    if _WEBSITE_URL
    else None
)

_oauth_provider: CiteCaddyOAuthProvider | None = None
_token_store: TokenStore | None = None

if _PORT:
    _http_settings = HttpSettings.from_env()
    _token_store = TokenStore(
        os.path.join(_http_settings.data_dir, "oauth_store.json"),
        fernet_key=_http_settings.token_store_key,
    )
    _oauth_provider = CiteCaddyOAuthProvider(store=_token_store)
    mcp = MCPServer(
        "Cite Caddy",
        instructions=_INSTRUCTIONS,
        website_url=_WEBSITE_URL,
        icons=_ICONS,
        version=_pkg_version("cite-caddy"),
        auth_server_provider=_oauth_provider,
        auth=AuthSettings(
            issuer_url=_http_settings.public_url,
            resource_server_url=f"{_http_settings.public_url}/mcp",
            client_registration_options=ClientRegistrationOptions(enabled=True),
            revocation_options=RevocationOptions(enabled=True),
        ),
    )
else:
    mcp = MCPServer(
        "Cite Caddy",
        instructions=_INSTRUCTIONS,
        website_url=_WEBSITE_URL,
        icons=_ICONS,
        version=_pkg_version("cite-caddy"),
    )


if _PORT:

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(request: Request) -> PlainTextResponse:
        return PlainTextResponse("OK")

    def _first_paragraph(text: str) -> str:
        """Collapses a tool docstring down to its first paragraph, with
        internal whitespace/newlines flattened to single spaces -- used
        for /.well-known/mcp/server-card.json below, where the full
        multi-paragraph docstring (meant for an MCP client's model, which
        reads the whole thing) would be needlessly verbose for a
        pre-connection discovery summary."""
        paragraph = text.strip().split("\n\n", 1)[0]
        return " ".join(paragraph.split())

    def _tool_card_entry(t: Tool) -> dict[str, Any]:
        """Full-fidelity per-tool entry for /.well-known/mcp/server-card.json
        -- includes outputSchema/annotations because every tool here
        already has both via its @mcp.tool() registration, so omitting
        them would just be throwing away metadata that's already sitting
        on `t`. exclude_none matches the SDK's own tools/list serialization
        (mcp/server/lowlevel/server.py) -- fields a tool doesn't have (e.g.
        no outputSchema, or an annotation hint left unset) are left out of
        the JSON entirely rather than emitted as an explicit `null`, which
        a strict client-side schema validator could reject."""
        d = t.model_dump(mode="json", by_alias=True, exclude_none=True)
        entry: dict[str, Any] = {
            "name": d["name"],
            "description": _first_paragraph(d.get("description") or ""),
            "inputSchema": d["inputSchema"],
        }
        if "outputSchema" in d:
            entry["outputSchema"] = d["outputSchema"]
        if "annotations" in d:
            entry["annotations"] = d["annotations"]
        return entry

    async def _server_card_data() -> dict[str, Any]:
        tools = await mcp.list_tools()
        server_info: dict[str, Any] = {
            "name": "Cite Caddy",
            "version": _pkg_version("cite-caddy"),
            "description": (
                "Remote MCP server for full read/write access to a Zotero library"
            ),
        }
        if _WEBSITE_URL:
            server_info["homepage"] = _WEBSITE_URL
            server_info["icon"] = f"{_WEBSITE_URL}icons/icon.svg"
        return {
            "serverInfo": server_info,
            "authentication": {"required": True, "schemes": ["oauth2"]},
            "tools": [_tool_card_entry(t) for t in tools],
            "resources": [],
            "prompts": [],
        }

    @mcp.custom_route("/.well-known/mcp/server-card.json", methods=["GET"])
    async def server_card(request: Request) -> JSONResponse:
        """A pragmatic approximation of SEP-2127 ("MCP Server Cards -- HTTP
        Server Discovery"), which is NOT a ratified part of the MCP spec as
        of this writing -- SEP-2127 (github.com/modelcontextprotocol/
        modelcontextprotocol/pull/2127, superseding the withdrawn SEP-1649)
        is still an open, unmerged proposal, and even its own well-known
        path has changed between drafts (some revisions use
        `.well-known/ai-catalog.json` instead of this one) -- it is NOT a
        claim of spec compliance, and the path/shape here may need to change
        if/when SEP-2127 (or a successor) actually ratifies with a different
        contract. Generated live from mcp.list_tools() on every request
        rather than a static file, so it can't drift out of sync with the real
        tool registry.
        """
        return JSONResponse(await _server_card_data())

    _GLAMA_JSON_PATH = Path(__file__).resolve().parent.parent / "glama.json"

    @mcp.custom_route("/.well-known/glama.json", methods=["GET"])
    async def glama_well_known(request: Request) -> JSONResponse:
        """Serves this repo's own glama.json (already read from GitHub by
        Glama.ai for the separate glama.ai/mcp/servers/... repo-based
        grading) at the live URL too -- required for the
        glama.ai/mcp/connectors/... Connector listing to verify ownership;
        without this, that page reports no ownership verification even
        though the maintainers list is right there in the repo."""
        return JSONResponse(json.loads(_GLAMA_JSON_PATH.read_text()))

    async def _track_tool_call(
        ctx: ServerRequestContext[Any, Any], call_next: CallNext
    ) -> HandlerResult:
        """Records every `tools/call` in app/metrics.py, by tool name only
        -- no tenant/library identity -- so `/status` below can report
        aggregate call/error counts. Registered on `mcp.middleware`, which
        wraps every inbound MCP message, so non-tool methods (initialize,
        list_tools, ...) are passed through untouched."""
        if ctx.method != "tools/call":
            return await call_next(ctx)
        name = (ctx.params or {}).get("name", "unknown")
        metrics.tool_calls[name] += 1
        try:
            return await call_next(ctx)
        except Exception:
            metrics.tool_errors[name] += 1
            raise

    mcp.middleware.append(_track_tool_call)

    def _status_data() -> dict[str, Any]:
        return {
            "version": _pkg_version("cite-caddy"),
            "uptime_seconds": metrics.uptime_seconds(),
            "tenants": _token_store.tenant_count() if _token_store else None,
            "tool_calls": dict(metrics.tool_calls),
            "tool_errors": dict(metrics.tool_errors),
        }

    @mcp.custom_route("/status", methods=["GET"])
    async def status(request: Request) -> JSONResponse:
        return JSONResponse(_status_data())

    def _status_page(data: dict[str, Any]) -> str:
        icon_html = ""
        if _WEBSITE_URL:
            icon_src = html.escape(f"{_WEBSITE_URL}icons/icon.svg")
            icon_html = f'<img src="{icon_src}" alt="" width="24" height="24">\n  '

        tool_names = sorted(set(data["tool_calls"]) | set(data["tool_errors"]))
        if tool_names:
            tool_rows = "\n".join(
                f"  <tr><td>{html.escape(name)}</td>"
                f"<td>{data['tool_calls'].get(name, 0)}</td>"
                f"<td>{data['tool_errors'].get(name, 0)}</td></tr>"
                for name in tool_names
            )
        else:
            tool_rows = (
                '  <tr><td colspan="3" class="hint">No tool calls yet.</td></tr>'
            )

        tenants = data["tenants"] if data["tenants"] is not None else "n/a"
        return f"""<!doctype html>
<title>Cite Caddy status</title>
<style>
  body {{ font-family: system-ui, sans-serif; max-width: 28rem; margin: 4rem auto; }}
  h1 {{ display: flex; align-items: center; gap: 0.5rem; }}
  img {{ border-radius: 6px; }}
  table {{ width: 100%; border-collapse: collapse; margin: 1.5rem 0; }}
  th, td {{ text-align: left; padding: 0.35rem 0.5rem; border-bottom: 1px solid #ddd; }}
  .hint {{ color: #666; font-size: 0.9em; }}
</style>
<h1>{icon_html}Cite Caddy</h1>
<table>
  <tr><th>Version</th><td>{html.escape(str(data["version"]))}</td></tr>
  <tr><th>Uptime</th><td>{data["uptime_seconds"]}s</td></tr>
  <tr><th>Tenants</th><td>{tenants}</td></tr>
</table>
<table>
  <tr><th>Tool</th><th>Calls</th><th>Errors</th></tr>
{tool_rows}
</table>
"""

    @mcp.custom_route("/status.html", methods=["GET"])
    async def status_html(request: Request) -> HTMLResponse:
        return HTMLResponse(_status_page(_status_data()))

    def _login_page(
        login_id: str,
        error: str | None = None,
        library_id: str = "",
        library_type: str = "user",
    ) -> str:
        error_html = f'<p class="error">{html.escape(error)}</p>' if error else ""
        selected = {
            t: " selected" if t == library_type else "" for t in ("user", "group")
        }
        icon_html = ""
        privacy_html = ""
        if _WEBSITE_URL:
            icon_src = html.escape(f"{_WEBSITE_URL}icons/icon.svg")
            icon_html = f'<img src="{icon_src}" alt="" width="48" height="48">\n  '
            privacy_href = html.escape(f"{_WEBSITE_URL}privacy.html")
            privacy_html = f'<p class="hint"><a href="{privacy_href}">Privacy policy</a> -- what this server stores and how to have it deleted.</p>\n'
        return f"""<!doctype html>
<title>Cite Caddy login</title>
<style>
  body {{ font-family: system-ui, sans-serif; max-width: 24rem; margin: 4rem auto; }}
  header {{ text-align: center; }}
  header img {{ border-radius: 10px; }}
  input, select {{ display: block; width: 100%; margin: 0.5rem 0 1rem; padding: 0.5rem; box-sizing: border-box; }}
  button {{ padding: 0.5rem 1.5rem; }}
  .error {{ color: #b00020; }}
  .hint {{ color: #666; font-size: 0.9em; }}
</style>
<header>
  {icon_html}<h1>Cite Caddy</h1>
</header>
<p>Connect this MCP client to your own Zotero library. Signing in with a
valid Zotero API key both grants access and registers your library with
this server -- no separate sign-up.</p>
{error_html}
<form method="post" action="/login">
  <input type="hidden" name="login_id" value="{html.escape(login_id)}">
  <label>Zotero Library ID
    <input type="text" name="library_id" value="{html.escape(library_id)}" autofocus required>
  </label>
  <label>Library Type
    <select name="library_type">
      <option value="user"{selected["user"]}>User</option>
      <option value="group"{selected["group"]}>Group</option>
    </select>
  </label>
  <label>API Key
    <input type="password" name="api_key" required>
  </label>
  <p class="hint">Find these at Zotero -> Settings -> Security -> Applications
  (the key needs write permission for this project).</p>
  <button type="submit">Sign in</button>
</form>
{privacy_html}"""

    @mcp.custom_route("/login", methods=["GET"])
    async def login_form(request: Request) -> HTMLResponse:
        login_id = request.query_params.get("login_id", "")
        try:
            assert _oauth_provider is not None
            _oauth_provider.get_pending(login_id)
        except LoginSessionExpiredError as e:
            return HTMLResponse(f"<p>{html.escape(str(e))}</p>", status_code=400)
        return HTMLResponse(_login_page(login_id))

    @mcp.custom_route("/login", methods=["POST"])
    async def login_submit(request: Request) -> Response:
        form = await request.form()
        login_id = str(form.get("login_id", ""))
        library_id = str(form.get("library_id", ""))
        library_type = str(form.get("library_type", "user"))
        api_key = str(form.get("api_key", ""))
        assert _oauth_provider is not None
        try:
            redirect_url = await _oauth_provider.complete_login(
                login_id, library_id, library_type, api_key
            )
        except LoginSessionExpiredError as e:
            return HTMLResponse(f"<p>{html.escape(str(e))}</p>", status_code=400)
        except InvalidCredentialsError as e:
            return HTMLResponse(
                _login_page(
                    login_id,
                    error=str(e),
                    library_id=library_id,
                    library_type=library_type,
                ),
                status_code=401,
            )
        return RedirectResponse(url=redirect_url, status_code=302)


_service: ZoteroService | None = None
_services: dict[str, ZoteroService] = {}


def get_service() -> ZoteroService:
    """Returns the ZoteroService for the current request.

    HTTP mode ($PORT set): per-tenant. Looks up the current request's
    bearer AccessToken (set by the SDK's auth middleware, via
    get_access_token()), reads its `subject` (the tenant's library_id --
    see app/oauth_provider.py's _issue_tokens), and builds/caches a
    ZoteroService for that tenant from app/oauth_store.py's TokenStore
    "tenants" collection. Rebuilds the cached entry if the stored api_key
    no longer matches (e.g. the tenant re-logged in with a rotated key).

    stdio mode (no $PORT): unchanged -- a single process-wide instance
    built from env vars, deferred past import time so importing this
    module (e.g. from tests) doesn't require ZOTERO_* env vars to be set;
    tests instead call configure_service() with a fake-backed
    ZoteroService.
    """
    if _PORT:
        token = get_access_token()
        if token is None or token.subject is None:
            raise RuntimeError("No authenticated Zotero tenant for this request.")
        assert _token_store is not None
        tenant = _token_store.get_tenant(token.subject)
        if tenant is None:
            raise RuntimeError(
                f"No stored Zotero credentials for tenant {token.subject!r}."
            )
        cached = _services.get(token.subject)
        if cached is None or cached.api_key != tenant["api_key"]:
            cached = ZoteroService.from_settings(
                Settings(
                    library_id=tenant["library_id"],
                    library_type=tenant["library_type"],
                    api_key=tenant["api_key"],
                )
            )
            _services[token.subject] = cached
        return cached

    global _service
    if _service is None:
        _service = ZoteroService.from_settings(Settings.from_env())
    return _service


def configure_service(service: ZoteroService) -> None:
    """Overrides the cached stdio-mode service -- used by tests to inject
    one backed by a fake Zotero client instead of a real pyzotero client."""
    global _service
    _service = service


# ---- read --------------------------------------------------------------


@mcp.tool(
    annotations=ToolAnnotations(
        title="Search Zotero Items",
        readOnlyHint=True,
        openWorldHint=True,
    )
)
def search_items(
    query: str = "",
    item_type: str | None = None,
    tag: str | None = None,
    collection_key: str | None = None,
    limit: int = 25,
    start: int = 0,
    full_text: bool = False,
) -> list[dict]:
    """Search the Zotero library. Read-only.

    query: substring match against title/creator/year (Zotero's quick
        search), or omit to list items.
    item_type: filter to one Zotero item type, e.g. "journalArticle",
        "book", "report", "webpage".
    tag: filter to items carrying this exact tag.
    collection_key: filter to items filed in this collection (see
        list_collections).
    limit/start: pagination (default limit 25).
    full_text: also match `query` against the indexed content of attached
        files and notes, not just title/creator/year (Zotero's
        qmode="everything"). Slower than the default search. Requires a
        non-empty query -- raises a validation error otherwise.

    Each result includes `key` and `version` -- pass both to update_item,
    add_tags/remove_tags/set_tags, add_to_collection/remove_from_collection,
    delete_item_permanently, or move_item_to_different_library. `creators`
    is a list of {creatorType, firstName, lastName} (or {creatorType,
    name} for single-field/institutional creators) entries, preserving
    each creator's role (author, editor, seriesEditor, translator,
    contributor, ...) -- the same shape create_item/update_item accept,
    so it can be passed straight back in.
    """
    return get_service().search_items(
        query=query,
        item_type=item_type,
        tag=tag,
        collection_key=collection_key,
        limit=limit,
        start=start,
        full_text=full_text,
    )


@mcp.tool(
    annotations=ToolAnnotations(
        title="Get Zotero Item",
        readOnlyHint=True,
        openWorldHint=True,
    )
)
def get_item(key: str) -> dict:
    """Fetch one item by key. Read-only. Use this to get an item's current
    `version` right before a mutating call, if you don't already have a
    fresh one from search_items. `creators` is a list of {creatorType,
    firstName, lastName} (or {creatorType, name} for single-field/
    institutional creators) entries -- see search_items' docstring."""
    return get_service().get_item(key)


@mcp.tool(
    annotations=ToolAnnotations(
        title="List Zotero Trash",
        readOnlyHint=True,
        openWorldHint=True,
    )
)
def list_trash(limit: int = 25, start: int = 0) -> list[dict]:
    """List items currently in the trash -- soft-deleted (e.g. via the
    Zotero desktop app's "Move to Trash", or trash_item), but not yet
    permanently gone. Read-only. Each result's `key`/`version` can be
    passed to restore_from_trash. limit/start: pagination (default limit
    25). `creators` shape matches search_items/get_item."""
    return get_service().list_trash(limit=limit, start=start)


@mcp.tool(
    annotations=ToolAnnotations(
        title="List Zotero Collections",
        readOnlyHint=True,
        openWorldHint=True,
    )
)
def list_collections() -> list[dict]:
    """List all collections in the library (key, name, parent_collection).
    Read-only."""
    return get_service().list_collections()


@mcp.tool(
    annotations=ToolAnnotations(
        title="List Zotero Saved Searches",
        readOnlyHint=True,
        openWorldHint=True,
    )
)
def list_saved_searches() -> list[dict]:
    """List saved searches -- Zotero's own stored search definitions
    (visible in the desktop app's left-hand pane), not ad-hoc calls to
    search_items. Read-only. Each result's `key` can be passed to
    delete_saved_search."""
    return get_service().list_saved_searches()


@mcp.tool(
    annotations=ToolAnnotations(
        title="List Zotero Groups",
        readOnlyHint=True,
        openWorldHint=True,
    )
)
def list_groups() -> list[dict]:
    """List the Zotero groups the configured API key's user account
    belongs to. Read-only. Each result's `id` can be passed as
    target_library_id (with target_library_type="group") to
    move_item_to_different_library, if the group you want isn't this
    server's own configured library."""
    return get_service().list_groups()


@mcp.tool(
    annotations=ToolAnnotations(
        title="List Zotero Item Types",
        readOnlyHint=True,
        openWorldHint=True,
    )
)
def list_item_types() -> list[dict]:
    """List every Zotero item type (e.g. "book", "journalArticle",
    "webpage") -- valid values for create_item's item_type argument.
    Read-only."""
    return get_service().list_item_types()


@mcp.tool(
    annotations=ToolAnnotations(
        title="List Zotero Item Fields",
        readOnlyHint=True,
        openWorldHint=True,
    )
)
def list_item_fields() -> list[dict]:
    """List every bibliographic field Zotero recognizes across all item
    types combined -- not which fields are valid for one specific type
    (see list_item_type_fields for that, which is what create_item
    actually needs). Read-only."""
    return get_service().list_item_fields()


@mcp.tool(
    annotations=ToolAnnotations(
        title="List Fields for a Zotero Item Type",
        readOnlyHint=True,
        openWorldHint=True,
    )
)
def list_item_type_fields(item_type: str) -> list[dict]:
    """List the bibliographic fields valid for one item type -- only
    these keys are valid in create_item/update_item's `fields` argument
    for this item_type; anything else raises a validation error.
    Read-only."""
    return get_service().list_item_type_fields(item_type)


@mcp.tool(
    annotations=ToolAnnotations(
        title="List Creator Types for a Zotero Item Type",
        readOnlyHint=True,
        openWorldHint=True,
    )
)
def list_item_creator_types(item_type: str) -> list[dict]:
    """List the valid `creatorType` values (e.g. "author", "editor") for
    one item type -- for create_item/update_item's `creators` entries,
    e.g. {"creatorType": "author", ...}. Read-only."""
    return get_service().list_item_creator_types(item_type)


@mcp.tool(
    annotations=ToolAnnotations(
        title="List Zotero Creator Fields",
        readOnlyHint=True,
        openWorldHint=True,
    )
)
def list_creator_fields() -> list[dict]:
    """List the name-shape fields Zotero recognizes on a `creators` entry
    (e.g. "firstName", "lastName", "name") -- for building creators
    entries in create_item/update_item's `creators` argument. Distinct
    from list_item_creator_types, which lists creatorType *roles*
    (author, editor, ...) rather than name fields. Read-only."""
    return get_service().list_creator_fields()


@mcp.tool(
    annotations=ToolAnnotations(
        title="List Zotero Tags",
        readOnlyHint=True,
        openWorldHint=True,
    )
)
def list_tags(query: str | None = None, limit: int = 100, start: int = 0) -> list[str]:
    """List distinct tags used anywhere in the library -- not one item's
    tags (see search_items/get_item for those). Read-only.

    query: substring filter on tag name, or omit to list all tags.
    limit/start: pagination (default limit 100).
    """
    return get_service().list_tags(query=query, limit=limit, start=start)


# ---- attachments & fulltext (read) ---------------------------------------


@mcp.tool(
    annotations=ToolAnnotations(
        title="List Item Attachments",
        readOnlyHint=True,
        openWorldHint=True,
    )
)
def list_attachments(item_key: str) -> list[dict]:
    """List the file attachments (PDFs, snapshots, etc.) filed under an
    item -- not its notes. Read-only. Each result's `key` can be passed to
    download_attachment or get_fulltext."""
    return get_service().list_attachments(item_key)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Get Attachment Full Text",
        readOnlyHint=True,
        openWorldHint=True,
    )
)
def get_fulltext(attachment_key: str) -> dict:
    """Fetch Zotero's extracted full-text content and indexing progress
    for an attachment (see list_attachments for keys). Only meaningful
    for attachments Zotero has indexed -- PDFs/text files with extracted
    text -- not e.g. images; raises an error if there's no indexed full
    text for this key. Read-only."""
    return get_service().get_fulltext(attachment_key)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Download Attachment",
        readOnlyHint=True,
        openWorldHint=True,
    )
)
def download_attachment(attachment_key: str) -> dict:
    """Download an attachment's file content (see list_attachments for
    keys). Read-only. Returns `content_base64` -- this server runs
    remotely, so raw bytes travel as a base64 string rather than a local
    file path; decode it to reconstruct the file."""
    return get_service().download_attachment(attachment_key)


@mcp.tool(
    annotations=ToolAnnotations(
        title="List Item Notes",
        readOnlyHint=True,
        openWorldHint=True,
    )
)
def list_notes(item_key: str) -> list[dict]:
    """List the notes filed under an item -- not its file attachments
    (see list_attachments for those). Read-only. Each result includes
    full note content and `version` -- pass both to update_note."""
    return get_service().list_notes(item_key)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Export Bibliography or Citations",
        readOnlyHint=True,
        openWorldHint=True,
    )
)
def export_bibliography(
    keys: list[str], style: str = "apa", format: str = "bibliography"
) -> dict:
    """Generate formatted bibliography/citation entries or portable export
    data for one or more items. Read-only.

    keys: item keys (see search_items/get_item). Keys not found in the
        library are simply absent from the result -- not an error.
    style: a Zotero/CSL style ID (e.g. "apa",
        "modern-language-association", "chicago-note-bibliography") --
        only used when format is "bibliography" or "citation"; ignored
        otherwise. An unknown style raises an error naming the problem.
    format:
        "bibliography" (default) -- HTML reference-list entries, one per
            found key, in `style`.
        "citation" -- HTML in-text citations, one per found key, in
            `style`.
        "csljson" -- structured CSL-JSON, one object per found key --
            portable, importable into other reference managers or format
            converters.
        "bibtex" -- one combined BibTeX text blob covering all found
            keys, ready to paste into a LaTeX project.

    Returns `content`, whose type depends on `format`: a list of strings
    for "bibliography"/"citation", a list of objects for "csljson", or a
    single string for "bibtex".
    """
    return get_service().export_bibliography(keys, style=style, format=format)


# ---- create (safe) -------------------------------------------------------


@mcp.tool(
    annotations=ToolAnnotations(
        title="Create Zotero Item",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    )
)
def create_item(
    item_type: str,
    fields: dict[str, str] | None = None,
    creators: list[dict[str, str]] | None = None,
    tags: list[str] | None = None,
    collections: list[str] | None = None,
) -> dict:
    """Add a new item to the library. Safe: creates a brand-new key, never
    touches an existing one.

    item_type: a Zotero item type, e.g. "journalArticle", "book", "report",
        "webpage", "conferencePaper".
    fields: bibliographic fields for that type, e.g. {"title": "...",
        "date": "2024", "DOI": "10.1/x", "url": "...", "abstractNote":
        "...", "publicationTitle": "..."}. Which fields are valid depends
        on item_type; an invalid field raises an error naming the problem.
    creators: e.g. [{"creatorType": "author", "firstName": "Ada",
        "lastName": "Lovelace"}].
    tags: plain tag strings.
    collections: collection keys (see list_collections) to file the new
        item into immediately.
    """
    return get_service().create_item(
        item_type, fields=fields, creators=creators, tags=tags, collections=collections
    )


@mcp.tool(
    annotations=ToolAnnotations(
        title="Create Zotero Collection",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    )
)
def create_collection(name: str, parent_key: str | None = None) -> dict:
    """Create a new collection, optionally nested under parent_key. Safe."""
    return get_service().create_collection(name, parent_key=parent_key)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Create Zotero Saved Search",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    )
)
def create_saved_search(name: str, conditions: list[dict[str, str]]) -> dict:
    """Create a new saved search. Safe: creates a brand-new key, never
    touches an existing one.

    conditions: a list of dicts, each with exactly the keys "condition",
    "operator", "value", e.g. [{"condition": "itemType", "operator": "is",
    "value": "journalArticle"}]. Which condition/operator combinations are
    valid is Zotero-defined and fairly extensive; an invalid combination
    raises an error naming the problem.
    """
    return get_service().create_saved_search(name, conditions)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Rename or Move Zotero Collection",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )
)
def update_collection(
    key: str, version: int, name: str | None = None, parent_key: str | None = None
) -> dict:
    """Rename and/or move (reparent) a collection, in place. Safe: the
    collection's key is unchanged, so items filed in it and any
    sub-collections stay put.

    name: new name; omit to leave the current name unchanged.
    parent_key: new parent collection's key, to nest this collection
        under it; pass "" (empty string) to move it to the top level (out
        of any parent); omit entirely to leave the parent unchanged. At
        least one of name/parent_key must be given.
    version: the collection's current version (from list_collections) --
        refused if stale, same as update_item.
    """
    return get_service().update_collection(
        key, version, name=name, parent_key=parent_key
    )


@mcp.tool(
    annotations=ToolAnnotations(
        title="Move Zotero Item to Trash",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=True,
        openWorldHint=True,
    )
)
def trash_item(key: str, version: int) -> dict:
    """Move an item to the trash (soft delete). Unlike
    delete_item_permanently, this is reversible via restore_from_trash --
    prefer it whenever a delete might need to be undone. Safe,
    key-preserving: the item's key is unchanged, so a Word citation
    referencing it keeps resolving unless/until it's later permanently
    deleted (e.g. via delete_item_permanently, or "Empty Trash" in the
    Zotero desktop app).

    version: the item's current version (from search_items/get_item) --
    refused if stale, same as update_item.
    """
    return get_service().trash_item(key, version)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Restore Zotero Item from Trash",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )
)
def restore_from_trash(key: str, version: int) -> dict:
    """Remove an item from the trash, restoring it to the library. Safe,
    key-preserving. version: the item's current version (from
    list_trash)."""
    return get_service().restore_from_trash(key, version)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Upload Attachment",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    )
)
def upload_attachment(
    parent_key: str, filename: str, content_base64: str, title: str | None = None
) -> dict:
    """Upload a new file attachment as a child of an existing item (e.g.
    attach a PDF to a journalArticle item). Safe: creates a brand-new
    attachment item with its own key; never touches the parent item's own
    fields or version.

    filename: name to store the file under, e.g. "paper.pdf" -- also used
        to guess Zotero's contentType from the extension.
    content_base64: the file's bytes, base64-encoded. This server runs
        remotely and has no access to the caller's local filesystem, so
        content must travel as a string rather than a local path.
    title: attachment title shown in Zotero; defaults to filename.
    """
    return get_service().upload_attachment(
        parent_key, filename, content_base64, title=title
    )


@mcp.tool(
    annotations=ToolAnnotations(
        title="Create Item Note",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    )
)
def create_note(parent_key: str, content: str, tags: list[str] | None = None) -> dict:
    """Add a new note as a child of an existing item (e.g. a research
    note attached to a journalArticle). Safe: creates a brand-new note
    item with its own key; never touches the parent item's own fields or
    version.

    content: the note's body, as Zotero-flavored HTML (e.g. "<p>Some
        observation.</p>") -- Zotero derives the note's display title
        from the first line of this content.
    tags: plain tag strings.
    """
    return get_service().create_note(parent_key, content, tags=tags)


# ---- update (safe, key-preserving) --------------------------------------


@mcp.tool(
    annotations=ToolAnnotations(
        title="Update Zotero Item Fields",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )
)
def update_item(key: str, version: int, fields: dict[str, Any]) -> dict:
    """Edit bibliographic fields (title, date, DOI, url, abstractNote,
    publicationTitle, creators, etc.) on an existing item, in place. Safe:
    the item's key is unchanged, so any Word citation referencing it keeps
    working. `creators` (if included) replaces the whole author/editor
    list -- e.g. [{"creatorType": "author", "firstName": "Ada",
    "lastName": "Lovelace"}], not a merge into the existing list.

    version: the item's current version (from search_items/get_item) --
    the edit is refused if this doesn't match the server's current version
    (someone else changed the item since you read it; re-fetch and retry).
    fields may NOT include tags/collections -- use add_tags/remove_tags/
    set_tags and add_to_collection/remove_from_collection for those.
    """
    return get_service().update_item(key, version, fields)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Update Zotero Item Publication Status",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )
)
def update_publication_status(
    key: str,
    version: int,
    fields: dict[str, Any],
    item_type: str | None = None,
    idempotency_key: str | None = None,
) -> dict:
    """Update an item in place to reflect that a preprint has been
    formally published -- e.g. an arXiv preprint that just received a
    journal DOI. Same key-preserving patch as update_item (Word citations
    keep working) -- prefer this or update_item over deleting and
    recreating the item whenever a preprint's status changes.

    Unlike update_item, `item_type` may also be passed to change the
    item's Zotero type (e.g. "preprint" -> "journalArticle") --
    update_item forbids that because it changes which fields are valid;
    this is the one tool where that's the deliberate point of the call.

    fields: same rules as update_item -- bibliographic fields such as DOI,
        url, date, publicationTitle, volume, issue, pages. May NOT include
        tags/collections/itemType/key/version -- use the dedicated tools
        for tags/collections, and item_type (not fields["itemType"]) to
        change the item type.
    item_type: new Zotero item type (see list_item_types); omit to leave
        it unchanged.
    version: the item's current version (from search_items/get_item) --
        refused if stale, same as update_item.
    idempotency_key: an opaque string you generate once per logical
        request. If a call with this exact key and these exact arguments
        already completed -- success OR error -- that same outcome is
        replayed instead of running anything against Zotero again, so
        retrying after a lost response (e.g. a timeout) can't turn one
        edit into two. Reusing a key with DIFFERENT arguments raises an
        error instead of silently returning the old result -- use a fresh
        key per distinct request.
    """
    return get_service().update_publication_status(
        key, version, fields, item_type=item_type, idempotency_key=idempotency_key
    )


@mcp.tool(
    annotations=ToolAnnotations(
        title="Update Item Note",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )
)
def update_note(key: str, version: int, content: str) -> dict:
    """Edit a note's content, in place. Safe, key-preserving.
    version: the note's current version (from list_notes)."""
    return get_service().update_note(key, version, content)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Add Tags to Zotero Item",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )
)
def add_tags(key: str, version: int, tags: list[str]) -> dict:
    """Add one or more tags to an item, keeping its existing tags. Safe,
    key-preserving. version: the item's current version (see update_item)."""
    return get_service().add_tags(key, version, tags)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Remove Tags from Zotero Item",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )
)
def remove_tags(key: str, version: int, tags: list[str]) -> dict:
    """Remove one or more tags from an item; other tags are kept. Safe,
    key-preserving. version: the item's current version (see update_item)."""
    return get_service().remove_tags(key, version, tags)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Set Zotero Item Tags",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )
)
def set_tags(key: str, version: int, tags: list[str]) -> dict:
    """Replace ALL of an item's tags with exactly this list (not merged --
    use add_tags/remove_tags to change tags incrementally instead). Safe,
    key-preserving. version: the item's current version (see update_item)."""
    return get_service().set_tags(key, version, tags)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Rename Zotero Tag",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )
)
def rename_tag(old_tag: str, new_tag: str) -> dict:
    """Rename a tag across every item in the library that carries it (not
    just one item -- see add_tags/remove_tags/set_tags for single-item
    edits). Zotero has no native tag-rename: this adds new_tag and removes
    old_tag on each affected item individually, merging with each item's
    existing tags.

    Not atomic across items -- if it fails partway through (e.g. a
    concurrent edit on one item), re-run with the same arguments;
    already-renamed items are skipped since they no longer carry old_tag.
    """
    return get_service().rename_tag(old_tag, new_tag)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Add Item to Zotero Collection",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )
)
def add_to_collection(key: str, version: int, collection_key: str) -> dict:
    """File an item into a collection, in addition to any it's already in.
    Safe, key-preserving reorganization within the same library -- prefer
    this over move_item_to_different_library whenever the goal is just
    organizing, not actually relocating to a different library.
    version: the item's current version (see update_item)."""
    return get_service().add_to_collection(key, version, collection_key)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Remove Item from Zotero Collection",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )
)
def remove_from_collection(key: str, version: int, collection_key: str) -> dict:
    """Remove an item from one collection; it stays in the library and any
    other collections it's filed under. Safe, key-preserving.
    version: the item's current version (see update_item)."""
    return get_service().remove_from_collection(key, version, collection_key)


# ---- destructive ----------------------------------------------------------


@mcp.tool(
    annotations=ToolAnnotations(
        title="Delete Zotero Item Permanently",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=True,
        openWorldHint=True,
    )
)
def delete_item_permanently(
    key: str, version: int, idempotency_key: str | None = None
) -> dict:
    """DESTRUCTIVE -- permanently deletes the item from its library. Cannot
    be undone through this server.

    Any Word document citing this item via the Zotero Word plugin's live
    field code references it by this key; deleting it breaks that citation
    silently -- the document won't show an error, it'll just show stale or
    broken text next time someone updates fields. Only call this when
    that's a known, accepted consequence, not as a routine cleanup step.

    version: the item's current version (from search_items/get_item) --
    the delete is refused if this doesn't match the server's current
    version, so a concurrent edit elsewhere isn't silently discarded along
    with the item.
    idempotency_key: optional opaque string, generated once per logical
    request. If a call with this exact key and these exact arguments
    already completed -- success OR error -- that same outcome is
    replayed instead of deleting (or trying to delete) anything again, so
    retrying after a lost response can't do this twice. Reusing a key
    with different arguments raises an error instead of silently
    returning the old result.
    """
    return get_service().delete_item(key, version, idempotency_key=idempotency_key)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Delete Zotero Tag",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=True,
        openWorldHint=True,
    )
)
def delete_tag(tag: str, idempotency_key: str | None = None) -> dict:
    """DESTRUCTIVE -- permanently removes this tag from every item in the
    library that carries it (not one item -- see remove_tags for that).
    Cannot be undone through this server.

    Unlike delete_item_permanently/delete_collection, this has no
    caller-supplied version to check -- Zotero's tag-delete endpoint is
    gated on the library's own version internally.
    idempotency_key: optional opaque string; if a call with this exact
    key and tag already completed, that same outcome is replayed instead
    of running against Zotero again -- see delete_item_permanently's
    docstring for the full explanation.
    """
    return get_service().delete_tag(tag, idempotency_key=idempotency_key)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Delete Zotero Collection",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=True,
        openWorldHint=True,
    )
)
def delete_collection(
    key: str, version: int, idempotency_key: str | None = None
) -> dict:
    """DESTRUCTIVE -- permanently deletes the collection. Matches
    Zotero's own "Delete Collection" behavior: any sub-collections nested
    under it are deleted too, cascading -- but items filed in it (or in a
    deleted sub-collection) are NOT deleted from the library, only
    unfiled from that collection.

    There is no confirmation step at this layer; check list_collections
    for sub-collections first if that matters before calling this.

    version: the collection's current version (from list_collections) --
    the delete is refused if this doesn't match the server's current
    version.
    idempotency_key: optional opaque string; if a call with this exact
    key and these exact arguments already completed, that same outcome
    is replayed instead of running against Zotero again -- see
    delete_item_permanently's docstring for the full explanation.
    """
    return get_service().delete_collection(
        key, version, idempotency_key=idempotency_key
    )


@mcp.tool(
    annotations=ToolAnnotations(
        title="Delete Zotero Saved Search",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=True,
        openWorldHint=True,
    )
)
def delete_saved_search(key: str, idempotency_key: str | None = None) -> dict:
    """DESTRUCTIVE, but low-risk -- permanently deletes this saved search
    definition. Unlike delete_item_permanently/delete_collection, this
    doesn't touch any items or their citations -- a saved search is just
    a stored filter, not a container.
    idempotency_key: optional opaque string; if a call with this exact
    key already completed, that same outcome is replayed instead of
    running against Zotero again -- see delete_item_permanently's
    docstring for the full explanation."""
    return get_service().delete_saved_search(key, idempotency_key=idempotency_key)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Move Zotero Item to Different Library",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=True,
    )
)
def move_item_to_different_library(
    key: str,
    version: int,
    target_library_id: str,
    target_library_type: str,
    idempotency_key: str | None = None,
) -> dict:
    """DESTRUCTIVE -- moves an item to a different Zotero library (e.g.
    from this user library into a group library, or vice versa). Zotero has
    no native cross-library move: this recreates the item in the target
    library under a BRAND-NEW key, then deletes the original.

    Any Word citation referencing the original key is broken by this,
    exactly like delete_item_permanently -- silently and permanently. If
    the goal is just reorganizing within THIS library, use
    add_to_collection/remove_from_collection instead -- those preserve the
    key and citations keep working.

    target_library_id/target_library_type: the destination library; the
    configured ZOTERO_API_KEY must have write access to it.
    version: the item's current version in the source library (from
    search_items/get_item) -- refused if stale.
    idempotency_key: strongly recommended for this tool specifically. If
    the create-in-target step succeeds but the delete-from-source step
    then fails, this operation ends up erroring while the item now exists
    in BOTH libraries -- a bare retry would redo the whole thing and
    create a SECOND duplicate in the target library, since the source
    item's version hasn't changed. Passing the same idempotency_key on
    retry replays the original failure (and its "clean up manually"
    guidance) instead of touching Zotero again. Also protects the normal
    success path the same way delete_item_permanently's does.

    Returns old_key and new_key -- report both to the caller so anyone
    relying on the old key knows it changed.
    """
    return get_service().move_item_to_library(
        key,
        version,
        target_library_id,
        target_library_type,
        idempotency_key=idempotency_key,
    )


def _http_transport_security(public_url: str) -> TransportSecuritySettings:
    """DNS-rebinding protection for streamable-http, scoped to this
    server's own public URL. The mcp SDK ships this middleware but leaves
    it disabled by default for backwards compatibility (see
    TransportSecurityMiddleware.__init__) -- it has to be opted into
    explicitly per deployment, which this server wasn't doing."""
    host = urlparse(public_url).netloc
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[host],
        allowed_origins=[public_url],
    )


def main() -> None:
    if not _PORT:
        Settings.from_env()  # stdio mode: fail fast at startup, not on first tool call
        mcp.run(transport="stdio")
    else:
        mcp.run(
            transport="streamable-http",
            host="0.0.0.0",
            port=int(_PORT),
            stateless_http=True,
            transport_security=_http_transport_security(_http_settings.public_url),
        )


if __name__ == "__main__":
    main()
