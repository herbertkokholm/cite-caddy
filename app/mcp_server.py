"""Remote MCP server exposing full read/write access to a Zotero library
(search, add, tag, update, delete, move, attachments, full text) -- see
README for the key safety note on delete/move breaking Word-plugin
citations.

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
what makes "Add custom connector" work at all. This server has exactly one
resource owner, so /authorize doesn't delegate to a third party --
completing a first-party login form (see /login below) against
MCP_AUTH_USERNAME/MCP_AUTH_PASSWORD is what gates access; see
app/oauth_provider.py's module docstring for the full flow.

Env vars (see README's "Configuration" section):
    ZOTERO_LIBRARY_ID      numeric library ID (user or group)
    ZOTERO_LIBRARY_TYPE    "user" or "group" (default: user)
    ZOTERO_API_KEY         needs write permission for this project
    MCP_AUTH_USERNAME      login-form username (HTTP mode only)
    MCP_AUTH_PASSWORD      login-form password (HTTP mode only)
    MCP_PUBLIC_URL         public HTTPS URL, e.g. https://your-domain.example
    MCP_DATA_DIR           where OAuth clients/tokens persist (default: ./.data)
"""

from __future__ import annotations

import html
import os
from typing import Any

from mcp.server.auth.settings import (
    AuthSettings,
    ClientRegistrationOptions,
    RevocationOptions,
)
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from starlette.requests import Request
from starlette.responses import (
    HTMLResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)

from app.config import AuthSettings as OwnerCredentials
from app.config import Settings, load_dotenv
from app.oauth_provider import (
    InvalidCredentialsError,
    LoginSessionExpiredError,
    ZoteroMCPOAuthProvider,
)
from app.oauth_store import TokenStore
from app.zotero_service import ZoteroService

load_dotenv()

_PORT = os.environ.get("PORT")
_INSTRUCTIONS = (
    "Full read/write access to a Zotero library: search, add, tag, update, "
    "delete, and move items and collections; list/download/upload item "
    "attachments and read their extracted full text. Mutating tools that "
    "touch an existing item require its current `version` (from "
    "search_items/get_item) and refuse the write if it's stale -- re-fetch "
    "and retry in that case, don't just resend the same version. "
    "delete_item_permanently and move_item_to_different_library break any "
    "Word-document citation referencing the item's key; prefer "
    "add_to_collection/remove_from_collection for in-library "
    "reorganization, which is safe. Attachment file content travels as "
    "base64 (upload_attachment's content_base64, download_attachment's "
    "content_base64 in the result) since this server runs remotely with "
    "no access to the caller's local filesystem."
)

_oauth_provider: ZoteroMCPOAuthProvider | None = None

if _PORT:
    _owner = OwnerCredentials.from_env()
    _oauth_provider = ZoteroMCPOAuthProvider(
        store=TokenStore(os.path.join(_owner.data_dir, "oauth_store.json")),
        username=_owner.username,
        password=_owner.password,
    )
    mcp = FastMCP(
        "zotero-mcp",
        instructions=_INSTRUCTIONS,
        host="0.0.0.0",
        port=int(_PORT),
        stateless_http=True,
        auth_server_provider=_oauth_provider,
        auth=AuthSettings(
            issuer_url=_owner.public_url,
            resource_server_url=f"{_owner.public_url}/mcp",
            client_registration_options=ClientRegistrationOptions(enabled=True),
            revocation_options=RevocationOptions(enabled=True),
        ),
    )
else:
    mcp = FastMCP("zotero-mcp", instructions=_INSTRUCTIONS)


if _PORT:

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(request: Request) -> PlainTextResponse:
        return PlainTextResponse("OK")

    def _login_page(login_id: str, error: str | None = None) -> str:
        error_html = f'<p class="error">{html.escape(error)}</p>' if error else ""
        return f"""<!doctype html>
<title>zotero-mcp login</title>
<style>
  body {{ font-family: system-ui, sans-serif; max-width: 24rem; margin: 4rem auto; }}
  input {{ display: block; width: 100%; margin: 0.5rem 0 1rem; padding: 0.5rem; box-sizing: border-box; }}
  button {{ padding: 0.5rem 1.5rem; }}
  .error {{ color: #b00020; }}
</style>
<h1>zotero-mcp</h1>
<p>Sign in to allow this MCP client access to your Zotero library.</p>
{error_html}
<form method="post" action="/login">
  <input type="hidden" name="login_id" value="{html.escape(login_id)}">
  <label>Username<input type="text" name="username" autofocus required></label>
  <label>Password<input type="password" name="password" required></label>
  <button type="submit">Sign in</button>
</form>
"""

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
        username = str(form.get("username", ""))
        password = str(form.get("password", ""))
        assert _oauth_provider is not None
        try:
            redirect_url = await _oauth_provider.complete_login(
                login_id, username, password
            )
        except LoginSessionExpiredError as e:
            return HTMLResponse(f"<p>{html.escape(str(e))}</p>", status_code=400)
        except InvalidCredentialsError as e:
            return HTMLResponse(_login_page(login_id, error=str(e)), status_code=401)
        return RedirectResponse(url=redirect_url, status_code=302)


_service: ZoteroService | None = None


def get_service() -> ZoteroService:
    """Lazily builds (and caches) the ZoteroService singleton from env vars.

    Deferred past import time so importing this module -- e.g. from tests
    -- doesn't require ZOTERO_* env vars to be set; tests instead call
    configure_service() with a fake-backed ZoteroService.
    """
    global _service
    if _service is None:
        _service = ZoteroService.from_settings(Settings.from_env())
    return _service


def configure_service(service: ZoteroService) -> None:
    """Overrides the cached service -- used by tests to inject one backed
    by a fake Zotero client instead of a real pyzotero client."""
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

    Each result includes `key` and `version` -- pass both to update_item,
    add_tags/remove_tags/set_tags, add_to_collection/remove_from_collection,
    delete_item_permanently, or move_item_to_different_library.
    """
    return get_service().search_items(
        query=query,
        item_type=item_type,
        tag=tag,
        collection_key=collection_key,
        limit=limit,
        start=start,
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
    fresh one from search_items."""
    return get_service().get_item(key)


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
def delete_item_permanently(key: str, version: int) -> dict:
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
    """
    return get_service().delete_item(key, version)


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
    key: str, version: int, target_library_id: str, target_library_type: str
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

    Returns old_key and new_key -- report both to the caller so anyone
    relying on the old key knows it changed.
    """
    return get_service().move_item_to_library(
        key, version, target_library_id, target_library_type
    )


def main() -> None:
    Settings.from_env()  # fail fast at startup, not on first tool call
    mcp.run(transport="streamable-http" if _PORT else "stdio")


if __name__ == "__main__":
    main()
