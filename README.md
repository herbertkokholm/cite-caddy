# zotero-mcp

A standalone, remote MCP server giving full read/write access to a Zotero
library — search, add, tag, update, delete, and move items; create, rename,
and delete collections; upload/download attachments and read their
extracted full text; read and write item notes.

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

Library to connect to is a setting, not hardcoded — this server should work
against whichever Zotero library its deployment is pointed at.

```
ZOTERO_LIBRARY_ID      numeric library ID (user or group)
ZOTERO_LIBRARY_TYPE    "user" or "group" (default: user)
ZOTERO_API_KEY         from Zotero -> Settings -> Security -> Applications
                        (needs write permission, not just read)
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
connector" work. This server has exactly one resource owner, so
`/authorize` doesn't delegate to a third-party identity provider — it
shows a first-party login form checked against `MCP_AUTH_USERNAME`/
`MCP_AUTH_PASSWORD`. Any MCP client can dynamically register itself (RFC
7591), but completing that login form is what actually gates access. See
`app/oauth_provider.py`'s module docstring for the full flow. Registered
clients and issued tokens persist to `MCP_DATA_DIR` (a Docker volume) so
redeploys don't log connected clients out.

`.env` on the host (not in this repo) holds `ZOTERO_LIBRARY_ID`/
`ZOTERO_LIBRARY_TYPE`/`ZOTERO_API_KEY` plus `MCP_AUTH_USERNAME`/
`MCP_AUTH_PASSWORD`/`MCP_PUBLIC_URL`, consumed via `docker-compose.yml`'s
`env_file:`.

`.github/workflows/deploy.yml` automates redeploying to an already
set-up host: manual trigger only (`workflow_dispatch`, never on push),
runs the test suite first, then syncs the repo over SSH and rebuilds the
container. It needs its own GitHub Actions **secrets** for the deploy SSH
key and target host/port/user — see the workflow file for the full list.
Use a dedicated deploy key (not whatever key you use for direct/manual
access), so it can be revoked independently if it ever leaks.

## Tools

Read-only: `search_items`, `get_item`, `list_collections`, `list_attachments`,
`get_fulltext`, `download_attachment`, `list_notes`.

Safe, key-preserving writes: `create_item`, `create_collection`,
`update_collection`, `update_item`, `add_tags`/`remove_tags`/`set_tags`,
`add_to_collection`/`remove_from_collection`, `upload_attachment`,
`create_note`, `update_note`.

Destructive (see "Key safety" above): `delete_item_permanently`,
`move_item_to_different_library`, `delete_collection`. All three require
the item's/collection's current Zotero `version` (get it from
`search_items`/`get_item`/`list_collections` first) and refuse the call if
it doesn't match the server's current version, rather than silently
overwriting a concurrent change. `delete_collection` cascades to any
sub-collections (matching Zotero's own "Delete Collection" behavior) but
never deletes the items filed in them.

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

Deployed and in active use. Add it as a remote MCP connector directly
(e.g. Claude Desktop/claude.ai's "Add custom connector" with just the
server's public URL) — the OAuth flow described above prompts for
username/password in-browser, no manually-configured headers needed.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## Security

See [SECURITY.md](SECURITY.md) for the threat model and how to report a
vulnerability.

## License

[MIT](LICENSE)
