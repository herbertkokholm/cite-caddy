# zotero-mcp

A standalone, remote MCP server giving full read/write access to a Zotero
library — search, add, tag, update, delete, and move items; create, rename,
and delete collections; upload/download attachments and read their
extracted full text; read and write item notes; manage tags, trash, and
saved searches library-wide; and look up Zotero's own item-type/field
schema. 36 tools total — see [Tools](#tools) below for the full list.

> Unofficial, community-maintained project. Not affiliated with, endorsed
> by, or sponsored by the Corporation for Digital Scholarship (Zotero) —
> see [Zotero's trademark policy](https://www.zotero.org/support/terms/trademark).

## Why this exists

Read-only tools that match findings against a Zotero library (e.g. by
DOI/arXiv ID) can safely stop at reporting — they never need to write
anything back. This project goes further on purpose: full CRUD against a
Zotero library, including delete and move, so that tagging, adding, and
cleaning up items can be automated too.

That's a deliberate scope choice, and it comes with a real risk: any write
that changes an existing item's *key* (delete, move to another library,
"clean library" reset) breaks Word documents that cite it via the Zotero
Word plugin's live field codes — see "Key safety" below before touching
delete/move.

## Key safety (read this before implementing delete/move)

Any Zotero item cited in a Word document via the Zotero Word plugin is
referenced by that item's **key**, embedded in a live field code. Operations
that preserve an item's key (create, update fields, add/remove tags, add
notes) are safe. Operations that don't (delete, and library-to-library move,
which Zotero implements as delete+recreate) will break those citations
silently — the Word document won't error, it'll just show stale/broken
field text next time someone updates fields or opens Zotero the next time.

Full CRUD was chosen deliberately for this project despite that risk. When
implementing delete/move tools:
- Make the destructive intent obvious in the tool name and docstring (an MCP
  client's model reads both before calling), not just in this README.
- Consider requiring the caller to pass back the item's current Zotero
  `version` (optimistic concurrency) so a delete/update can't silently clobber
  a change made concurrently from the Zotero desktop app or another client.
- A dry-run / confirmation step for delete is worth considering, but is an
  implementation decision for whoever builds that tool, not decided here.

## Configuration

**stdio mode** (local, single-user — no `$PORT`): the library to connect to
comes from env vars.

```
ZOTERO_LIBRARY_ID      numeric library ID (user or group)
ZOTERO_LIBRARY_TYPE    "user" or "group" (default: user)
ZOTERO_API_KEY         from Zotero -> Settings -> Security -> Applications
                        (needs write permission, not just read)
```

**HTTP mode** (remote, multi-tenant — `$PORT` set): there's no single
configured library — each caller brings their own Zotero Library ID/Type/API
Key via the `/login` form (see "Deployment" below). Instead:

```
MCP_TOKEN_STORE_KEY    Fernet key encrypting onboarded tenants' API keys at
                        rest; generate once at deploy time with:
                        python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
MCP_PUBLIC_URL          public HTTPS URL this server is reachable at
MCP_DATA_DIR            where OAuth clients/tokens/tenants persist (default: ./.data)
```

Optional in either mode:

```
MCP_WEBSITE_URL         public site reported as serverInfo.website_url; also used to
                        build serverInfo.icons[0].src as MCP_WEBSITE_URL + "icons/icon.svg"
                        (that file must actually be served there). Left unset, both
                        fields are simply omitted.
```

## Deployment

Ships as its own Docker container (`docker-compose.yml`), meant to sit
behind a reverse proxy that terminates TLS and forwards to the container's
port on localhost. Remote/hosted by default, not a local stdio server — an
MCP client just points at the URL, nothing to install or run locally.

**Access is gated by a real OAuth 2.1 authorization server built into the
app itself** (`app/oauth_provider.py`), not HTTP Basic Auth in front of it.
This is a deliberate design choice: Claude Desktop/claude.ai's "Add custom
connector" UI is OAuth-first — it always tries the OAuth discovery +
authorization-code dance against a new server, so a plain 401 in front of
the server (as Basic Auth would produce) gets read as "this server needs
OAuth" and fails once it hits a nonexistent `/authorize` endpoint.
Implementing a real (if minimal) OAuth server is what makes "Add custom
connector" work.

**Multi-tenant and self-service**: `/authorize` doesn't delegate to a
third-party identity provider — it shows a first-party login form asking
for a Zotero Library ID, Library Type, and API Key. Submitting the form
validates the key directly against the Zotero API; a successful
validation both grants access and registers ("onboards") that library as
a tenant of this server, all in one step — there's no separate sign-up
and no admin approval. Any MCP client can dynamically register itself
(RFC 7591), but completing the login form with a working Zotero key is
what actually gates access. Each caller's tool calls are then routed to
their own Zotero library, not a shared one. See
`app/oauth_provider.py`'s module docstring for the full flow. Registered
clients, issued tokens, and onboarded tenants' credentials (API keys
encrypted at rest with `MCP_TOKEN_STORE_KEY`) persist to `MCP_DATA_DIR`
(a Docker volume) so redeploys don't log connected clients out or forget
onboarded tenants.

`.env` on the host (not in this repo) holds `MCP_TOKEN_STORE_KEY`/
`MCP_PUBLIC_URL`, consumed via `docker-compose.yml`'s `env_file:`.
`ZOTERO_LIBRARY_ID`/`ZOTERO_LIBRARY_TYPE`/`ZOTERO_API_KEY` are not needed
for the HTTP deployment — those only apply to stdio mode.

`.github/workflows/deploy.yml` automates redeploying to an already
set-up host: manual trigger only (`workflow_dispatch`, never on push),
runs the test suite first, then syncs the repo over SSH and rebuilds the
container. It needs its own GitHub Actions **secrets** for the deploy SSH
key and target host/port/user — see the workflow file for the full list.
Use a dedicated deploy key (not whatever key you use for direct/manual
access), so it can be revoked independently if it ever leaks.

## Tools

Read-only: `search_items`, `get_item`, `list_collections`, `list_tags`,
`list_trash`, `list_saved_searches`, `list_groups`, `list_item_types`,
`list_item_fields`, `list_item_type_fields`, `list_item_creator_types`,
`list_attachments`, `get_fulltext`, `download_attachment`, `list_notes`.
`list_groups` lists the groups the configured API key's account belongs
to; its `id` doubles as `target_library_id` (with
`target_library_type="group"`) for `move_item_to_different_library`.
`list_item_type_fields`/`list_item_creator_types` describe what
`create_item`/`update_item` will actually accept for a given
`item_type` -- check them instead of guessing when a validation error
comes back. `search_items`' `query` defaults to Zotero's quick search
(title/creator/year); pass `full_text=True` to also match the indexed
content of attached files and notes (Zotero's `qmode="everything"`),
requiring a non-empty `query`.

Safe, key-preserving writes: `create_item`, `create_collection`,
`create_saved_search`, `update_collection`, `update_item`,
`add_tags`/`remove_tags`/`set_tags`, `rename_tag`,
`add_to_collection`/`remove_from_collection`, `trash_item`,
`restore_from_trash`, `upload_attachment`, `create_note`, `update_note`.
`trash_item` is a reversible soft delete (undo with `restore_from_trash`)
-- unlike `delete_item_permanently` below, it doesn't break Word
citations unless the item is later permanently deleted or the trash is
emptied.

Destructive (see "Key safety" above): `delete_item_permanently`,
`move_item_to_different_library`, `delete_collection`, `delete_tag`,
`delete_saved_search`. The first three require the item's/collection's
current Zotero `version` (get it from
`search_items`/`get_item`/`list_collections` first) and refuse the call if
it doesn't match the server's current version, rather than silently
overwriting a concurrent change. `delete_collection` cascades to any
sub-collections (matching Zotero's own "Delete Collection" behavior) but
never deletes the items filed in them. `rename_tag`/`delete_tag` act on
every item in the library carrying that tag, not just one -- there's no
per-tag version to check, since tags aren't standalone versioned entities
in Zotero's API. `delete_saved_search` is destructive but low-risk: a
saved search is just a stored filter, so deleting one never touches items
or citations.

Attachment file content (`upload_attachment`'s `content_base64` argument,
`download_attachment`'s `content_base64` result) travels base64-encoded —
this server is remote and has no access to the caller's local filesystem, so
raw bytes can't be passed as a local path the way pyzotero itself expects.

## Testing

```
uv venv && source .venv/bin/activate
uv pip install -e ".[dev]"
pytest
```

Tests never call a live Zotero library, even if `.env` has real
credentials: `app/zotero_service.py` (all Zotero read/write logic) is
exercised against `tests/fakes.py`'s in-memory `FakeZotero`, and
`app/mcp_server.py`'s tool functions are tested directly against a
`ZoteroService` backed by that fake (see `configure_service()`).

## Status

**v1.0** — deployed and in active use, with full CRUD coverage of the
Zotero Web API's item/collection/tag/trash/saved-search/schema surface
(36 tools; see [Tools](#tools)). Add it as a remote MCP connector directly
(e.g. Claude Desktop/claude.ai's "Add custom connector" with just the
server's public URL) — the OAuth flow described above prompts for your
own Zotero Library ID/Type/API Key in-browser, no manually-configured
headers needed, and no admin sign-up step.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## Security

See [SECURITY.md](SECURITY.md) for the threat model and how to report a
vulnerability.

## License

[MIT](LICENSE)
