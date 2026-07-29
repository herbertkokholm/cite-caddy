# Security Policy

## Supported versions

Tagged releases (starting with v1.0.0) mark milestones, but this project
is continuously deployed from `main` — only the latest commit on `main`
is supported. There's no backporting fixes to older tags.

## Reporting a vulnerability

Please don't open a public issue for a security vulnerability. Instead:

1. Preferred: use GitHub's private reporting — go to the
   [Security tab](../../security) → "Report a vulnerability".
2. Or email the address on the maintainer's GitHub profile with details.

This is a small, solo-maintained project, so responses are best-effort —
there's no guaranteed SLA, but reports will be looked at and, if valid,
fixed and disclosed once a fix is out.

## Scope

Relevant things to know when assessing impact:

- This server has **full read/write access** (search, add, tag, update,
  delete, move) to whichever Zotero library a tenant onboards — not
  read-only. A flaw that let a request bypass authentication, or let one
  tool call do something its docstring doesn't describe, is a security
  issue here in a way it wouldn't be for a read-only integration.
- **Multi-tenant**: any caller can self-serve onto this server with their
  own Zotero Library ID/API Key (`app/oauth_provider.py`'s
  `complete_login`, validated live against the Zotero API). A bug that let
  one tenant's tool calls resolve to a *different* tenant's Zotero
  library/API key (see `app/mcp_server.py`'s `get_service()`, which keys
  off the bearer token's `subject`) would be a critical cross-tenant data
  leak/corruption issue — this is the property most worth scrutinizing in
  this codebase.
- Access is gated by a first-party OAuth 2.1 login form
  (`app/oauth_provider.py`) — not by client_id/redirect_uri matching, which
  this server deliberately doesn't enforce strictly for auto-registered
  clients (see `_FlexibleClientInformation`'s docstring for why that
  trade-off was made). A bug in the login-form gate itself, or in
  access-token verification, is high severity.
- Onboarded tenants' Zotero API keys are encrypted at rest
  (`app/oauth_store.py`'s `TokenStore`, via `MCP_TOKEN_STORE_KEY`,
  a Fernet key). A bug that stored a key in plaintext, or that let one
  tenant's stored key be read back through another tenant's session, is a
  security issue.
- `delete_item_permanently` and `move_item_to_different_library` are
  explicitly destructive: they change or remove an item's Zotero *key*,
  which silently breaks any Word document citing that item via the Zotero
  Word plugin's live field codes (see README's "Key safety" section). A
  bug that caused either of these to run without the caller's intent, or
  without version-checked concurrency, would be treated as a security
  issue, not just a correctness bug.
- OAuth-login state (`MCP_TOKEN_STORE_KEY`, `MCP_PUBLIC_URL`) is supplied
  via environment variables (`.env` locally, platform/CI secrets in a
  deployment) and never committed; see
  [`CONTRIBUTING.md`](CONTRIBUTING.md#data-and-secrets). Tenants' own
  Zotero API keys are supplied at runtime through the login form, not via
  env vars, and are encrypted at rest as noted above.
