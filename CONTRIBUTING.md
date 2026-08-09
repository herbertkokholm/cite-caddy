# Contributing

Contributions are welcome — new tools, bug fixes, and improvements to the
OAuth/deployment story are all useful. Read `README.md` first for how the
pieces fit together, and the "Key safety" section in particular before
touching anything that deletes or moves an item.

## Project layout

All Python source lives under `app/` (a single package):

- `app/config.py` — env-var settings: `Settings` (stdio-mode Zotero
  credentials) and `HttpSettings` (HTTP-mode server config: public URL,
  data dir, the token-store encryption key). HTTP mode has no
  server-wide Zotero credentials of its own -- each tenant supplies
  their own at login instead (see below).
- `app/zotero_service.py` — all Zotero read/write logic, wrapped around
  `pyzotero`. No `mcp` dependency, so it's testable on its own.
- `app/oauth_store.py` / `app/oauth_provider.py` — this server's own OAuth
  2.1 authorization server (see `app/oauth_provider.py`'s module
  docstring for why it exists instead of a simpler auth scheme), plus
  self-service multi-tenant onboarding: `complete_login` validates a
  submitted Zotero API key live and persists it as a tenant, keyed by
  `library_id` and encrypted at rest.
- `app/mcp_server.py` — wires the above into `MCPServer` tools.

`tests/` mirrors this 1:1, plus `tests/fakes.py`'s in-memory fake Zotero
client.

## Running locally

Needs Python 3.10+.

```bash
uv venv && source .venv/bin/activate
uv pip install -e ".[dev]"
pytest
```

To run the server itself locally over stdio (no `$PORT` set, no OAuth):

```bash
ZOTERO_LIBRARY_ID=... ZOTERO_API_KEY=... python -m app.mcp_server
```

## Tests

```bash
pytest
```

**Never call a live Zotero library from a test**, even if you have real
credentials in `.env` — `app/zotero_service.py` is tested against
`tests/fakes.py`'s `FakeZotero`, and `app/mcp_server.py`'s tools are tested
directly against a `ZoteroService` backed by that fake (see
`configure_service()`). Add a new method to `FakeZotero` if you need
`pyzotero` behavior it doesn't yet emulate, rather than reaching for a
real client or a generic mock.

Add or update a test alongside any change to `app/zotero_service.py` or
`app/oauth_provider.py` — those carry almost all of this project's actual
logic.

## Code style

CI (`.github/workflows/lint.yml`) runs `ruff check` and `ruff format
--check` on every push and PR; run `ruff check .` and `ruff format .`
locally before pushing.

## Adding a new tool

Every mutating tool that touches an *existing* item must take the item's
current `version` and refuse the call if it doesn't match the server's
current version (see `app/zotero_service.py`'s module docstring for why —
in short, it's how a concurrent edit from the Zotero desktop app doesn't
get silently clobbered). If the tool can break a citation embedded in a
Word document (deleting an item, or anything that changes its key), name
it so the destructive intent is obvious and say so plainly in the
docstring — an MCP client's model reads the tool name and docstring before
deciding whether to call it, so that's the only place a warning reliably
reaches it.

Destructive or multi-step tools should also accept an optional
`idempotency_key: str | None = None` and thread it through
`ZoteroService._run_idempotent` (see `delete_item`/`move_item_to_library`
for examples). It replays a call's cached outcome — success *or* error —
instead of re-running it, which matters most for multi-step operations
where a retry after a partial failure would otherwise repeat side effects
that already happened (see README's "Idempotency" section).

## Data and secrets

Real Zotero/OAuth credentials belong in `.env` (gitignored) locally, or
platform/CI secrets in a deployment — never committed. Deploying via
`.github/workflows/deploy.yml` needs its own set of GitHub Actions
**secrets** (not repository variables — variables aren't masked in
workflow logs); see the workflow file for the full list.
