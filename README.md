# Cite Caddy

[![cite-caddy MCP server](https://glama.ai/mcp/servers/herbertkokholm/cite-caddy/badges/score.svg)](https://glama.ai/mcp/servers/herbertkokholm/cite-caddy)
[![smithery badge](https://smithery.ai/badge/herbertkokholm/cite-caddy)](https://smithery.ai/servers/herbertkokholm/cite-caddy)
[![MCP Registry](https://img.shields.io/badge/MCP%20Registry-listed-blue)](https://registry.modelcontextprotocol.io)
[![Lint](https://img.shields.io/github/actions/workflow/status/herbertkokholm/cite-caddy/lint.yml?branch=main&label=lint)](https://github.com/herbertkokholm/cite-caddy/actions/workflows/lint.yml)
[![Latest release](https://img.shields.io/github/v/release/herbertkokholm/cite-caddy)](https://github.com/herbertkokholm/cite-caddy/releases)
[![License: MIT](https://img.shields.io/github/license/herbertkokholm/cite-caddy)](LICENSE)

A reference-library bridge for AI assistants — a standalone, remote MCP
server.

> Independent, unofficial project. Not affiliated with, endorsed by, or
> sponsored by the Corporation for Digital Scholarship (Zotero) —
> see [Zotero's trademark policy](https://www.zotero.org/support/terms/trademark).
> Built on the Zotero Web API; today's backend is Zotero, but the name and
> tool surface are meant to support others later.

Cite Caddy gives full read/write access to a Zotero library — search, add,
tag, update, delete, and move items; create, rename, and delete
collections; upload/download attachments and read their extracted full
text; read and write item notes; manage tags, trash, and saved searches
library-wide; and look up Zotero's own item-type/field schema. 39 tools
total — see [Tools](#tools) below for the full list.

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

## Idempotency

`delete_item_permanently`, `delete_collection`, `delete_tag`,
`delete_saved_search`, `move_item_to_different_library`, and
`update_publication_status` all accept an optional `idempotency_key`. Pass
the same opaque string when retrying a call after a lost or ambiguous
response (e.g. a network timeout) and the *original* outcome — success or
error — is replayed instead of running the operation against Zotero again.
Reusing a key for a call with different arguments raises an error instead of
silently returning the old result, so it's safe to generate one key per
logical request and reuse it freely on retries of that same request.

This matters most for `move_item_to_different_library`: it recreates the
item in the target library, then deletes it from the source. If the create
succeeds but the delete then fails, a bare retry would redo the whole thing
— since the source item's version hasn't changed — creating a *second*
duplicate in the target library. With `idempotency_key`, the retry replays
the cached failure (and its "clean up manually" guidance) instead of
touching Zotero again.

The cache is in-memory per server process (per onboarded tenant in HTTP
mode), with a 24h TTL — it survives retries within that window, not across a
redeploy/restart.

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
                        and the /login page's privacy policy link as
                        MCP_WEBSITE_URL + "privacy.html" (both files must actually be
                        served there). Left unset, all three are simply omitted.
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

## Monitoring

Four unauthenticated GET endpoints, HTTP mode only (all require `$PORT`,
same as `/login`):

- **`/healthz`** — plain `200 OK`, for a load balancer/uptime check.
- **`/status`** — JSON snapshot of aggregate, process-level activity:

  ```json
  {
    "version": "2.1.0",
    "uptime_seconds": 41213,
    "tenants": 7,
    "tool_calls": {"search_items": 512, "add_tags": 41},
    "tool_errors": {"add_tags": 2}
  }
  ```

  `tenants` is `TokenStore.tenant_count()` — just the number of onboarded
  Zotero libraries. `tool_calls`/`tool_errors` are per-tool-name counts
  across *all* tenants combined, recorded by a `tools/call` middleware
  (`_track_tool_call` in `app/mcp_server.py`). Deliberately no per-tenant or
  per-library breakdown anywhere in this response — that's what keeps it
  safe to leave unauthenticated, unlike the 39 tools themselves. Counters
  live in `app/metrics.py`, in-memory only: they reset to zero on every
  restart/redeploy, same as this isn't a metrics/analytics system, just a
  lightweight "is it up and roughly how busy is it" signal.
- **`/status.html`** — same data as `/status`, rendered as a small page
  (name + icon, one table of version/uptime/tenants, one table of
  per-tool call/error counts) for a human checking in a browser rather
  than a script. Icon only renders when `MCP_WEBSITE_URL` is set, same
  as `/login`'s.
- **`/.well-known/mcp/server-card.json`** — a pre-connection discovery
  document (server identity, auth requirements, and the full tool list
  with schemas), generated live from the actual tool registry on every
  request so it can't drift out of sync. **Not a ratified standard**: this
  is a pragmatic approximation of
  [SEP-2127](https://github.com/modelcontextprotocol/modelcontextprotocol/pull/2127)
  ("MCP Server Cards — HTTP Server Discovery", superseding the withdrawn
  SEP-1649), which is still an open, unmerged proposal as of this
  writing — even its own well-known path has changed between drafts (some
  revisions use `.well-known/ai-catalog.json` instead) — it's not a general
  claim of spec compliance, and may need to change if/when SEP-2127 (or a
  successor) actually ratifies with a different contract.

## Tools

39 tools total, grouped by risk (see "Key safety" above before using any
Destructive tool). Tools marked **✓** under **Version** require the
item's/collection's current Zotero `version` (from
`search_items`/`get_item`/`list_collections`) as an argument and refuse
the call if it's stale, rather than silently overwriting a concurrent
change.

| Tool | Category | Version | Notes |
|---|---|:---:|---|
| `search_items` | Read-only | | `query` defaults to Zotero's quick search (title/creator/year); `full_text=True` also matches indexed content of attached files/notes (`qmode="everything"`), requires non-empty `query`. Each result's `creators` is a list of `{creatorType, firstName, lastName}` (or `{creatorType, name}` for single-field/institutional creators) entries — same shape `create_item`/`update_item` accept, preserving role (author vs. editor vs. translator, ...). |
| `get_item` | Read-only | | `creators` shape as above. |
| `list_collections` | Read-only | | |
| `list_tags` | Read-only | | |
| `list_trash` | Read-only | | `creators` shape as above. |
| `list_saved_searches` | Read-only | | |
| `list_groups` | Read-only | | `id` doubles as `target_library_id` (with `target_library_type="group"`) for `move_item_to_different_library`. |
| `list_item_types` | Read-only | | |
| `list_item_fields` | Read-only | | |
| `list_item_type_fields` | Read-only | | Check before `create_item`/`update_item` instead of guessing — what fields a given `item_type` accepts. |
| `list_item_creator_types` | Read-only | | Same, for `creators` entries' `creatorType`. |
| `list_creator_fields` | Read-only | | Name-shape fields (`firstName`, `lastName`, `name`, ...) valid on a `creators` entry itself — not the same as `list_item_creator_types` (roles). |
| `list_attachments` | Read-only | | |
| `get_fulltext` | Read-only | | |
| `download_attachment` | Read-only | | Content returned as `content_base64` — server has no access to the caller's local filesystem. |
| `list_notes` | Read-only | | |
| `export_bibliography` | Read-only | | Formatted HTML bibliography/citation entries (in a given CSL `style`) or portable export data (`csljson`, `bibtex`) for a list of item keys. Unknown keys silently omitted. |
| `create_item` | Safe write | | |
| `create_collection` | Safe write | | |
| `create_saved_search` | Safe write | | |
| `update_collection` | Safe write | ✓ | |
| `update_item` | Safe write | ✓ | |
| `update_publication_status` | Safe write | ✓ | Preprint → published: patches fields and, uniquely, `item_type` in place. Accepts `idempotency_key` — see "Idempotency" above. |
| `add_tags` | Safe write | ✓ | |
| `remove_tags` | Safe write | ✓ | |
| `set_tags` | Safe write | ✓ | |
| `rename_tag` | Safe write | | Library-wide — acts on every item carrying the tag, not just one; no per-tag version. Can block on large libraries: [#6](https://github.com/herbertkokholm/cite-caddy/issues/6). |
| `add_to_collection` | Safe write | ✓ | |
| `remove_from_collection` | Safe write | ✓ | |
| `trash_item` | Safe write | ✓ | Reversible soft delete — undo with `restore_from_trash`; doesn't break Word citations unless later permanently deleted or the trash is emptied. |
| `restore_from_trash` | Safe write | ✓ | |
| `upload_attachment` | Safe write | | Content sent as `content_base64` — server has no access to the caller's local filesystem. |
| `create_note` | Safe write | | |
| `update_note` | Safe write | ✓ | |
| `delete_item_permanently` | Destructive | ✓ | Breaks Word citations. Accepts `idempotency_key` — see "Idempotency" above. |
| `move_item_to_different_library` | Destructive | ✓ | Recreates the item under a brand-new key in the target library, then deletes the original — breaks Word citations. Accepts `idempotency_key`, strongly recommended here — see "Idempotency" above. |
| `delete_collection` | Destructive | ✓ | Cascades to sub-collections (matching Zotero's own "Delete Collection"); never deletes the items filed in them. Accepts `idempotency_key`. |
| `delete_tag` | Destructive | | Library-wide — acts on every item carrying the tag, not just one; no per-tag version. Accepts `idempotency_key`. |
| `delete_saved_search` | Destructive | | Low-risk — a saved search is just a stored filter, never touches items or citations. Accepts `idempotency_key`. |

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

**v2.3** — deployed and in active use, with full CRUD coverage of the
Zotero Web API's item/collection/tag/trash/saved-search/schema surface
(39 tools; see [Tools](#tools)). Add it as a remote MCP connector directly
(e.g. Claude Desktop/claude.ai's "Add custom connector" with just the
server's public URL) — the OAuth flow described above prompts for your
own Zotero Library ID/Type/API Key in-browser, no manually-configured
headers needed, and no admin sign-up step.

Listed in the official [MCP Registry](https://registry.modelcontextprotocol.io)
as `dk.herbertkokholm.citecaddy/cite-caddy` — metadata lives in
[`server.json`](server.json), published via `mcp-publisher` and DNS-verified
against `citecaddy.herbertkokholm.dk`. Not (yet) part of GitHub's separate,
manually-curated [github.com/mcp](https://github.com/mcp) directory, which
doesn't sync automatically from the open registry.

## Known limitations

Tracked gaps against the MCP [2026-07-28 specification](https://blog.modelcontextprotocol.io/posts/2026-07-28/)
("stateless core, enterprise authorization, extensions framework"). None
are currently exploitable or user-facing — each is either inert until an
upstream `mcp` SDK change, or already mitigated — but are documented here
so they're visibly known rather than silently absent.

- **OAuth authorization-response `iss` param (RFC 9207) not sent.** The
  spec hardens the OAuth flow against mix-up attacks by having the
  authorization server include an `iss` parameter in the redirect back to
  the client ([RFC 9207 §2.4](https://www.rfc-editor.org/rfc/rfc9207.html#section-2.4)),
  which spec-compliant clients then validate. This server's `/login` flow
  builds its final redirect by hand in `complete_login()`
  ([`app/oauth_provider.py`](app/oauth_provider.py)) rather than through
  the `mcp` SDK's built-in authorize handler, and currently omits `iss`.
  Harmless today: the installed `mcp` SDK (`mcp>=2.0.0,<3` in
  `pyproject.toml`) never advertises
  `authorization_response_iss_parameter_supported` in this server's OAuth
  metadata, so no compliant client requires it yet. Revisit if a future
  SDK version turns that advertisement on by default.

- **Dynamic Client Registration (RFC 7591) instead of CIMD.** The same
  spec update formally deprecates Dynamic Client Registration in favor of
  Client ID Metadata Documents (CIMD), though DCR remains functional for
  backward compatibility. This server's client auto-provisioning
  (`_FlexibleClientInformation`/`register_client`/`get_client` in
  [`app/oauth_provider.py`](app/oauth_provider.py)) is built on DCR —
  needed because some MCP clients (observed: Claude Desktop/claude.ai)
  skip registration and send `/authorize` an unregistered `client_id`
  directly (see that class's docstring). No action needed while the
  installed SDK keeps DCR working without warning; will need a
  CIMD-based replacement if/when that changes.

- **`rename_tag` can block on large libraries** — tracked as
  [#6](https://github.com/herbertkokholm/cite-caddy/issues/6); candidate
  for the spec's new `tasks` extension once the installed SDK exposes
  one.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## Security

See [SECURITY.md](SECURITY.md) for the threat model and how to report a
vulnerability.

## Privacy

See [Privacy Policy](https://herbertkokholm.dk/cite-caddy/privacy.html) for
what the server stores when you sign in at `/login` (Zotero Library
ID/Type/API key), how it's protected, and how to have it deleted.

## License

[MIT](LICENSE)
