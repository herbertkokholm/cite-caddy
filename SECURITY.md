# Security Policy

## Supported versions

This project doesn't do versioned releases — only the latest commit on
`main` is supported.

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
  delete, move) to whichever Zotero library it's configured against — not
  read-only. A flaw that let a request bypass authentication, or let one
  tool call do something its docstring doesn't describe, is a security
  issue here in a way it wouldn't be for a read-only integration.
- Access is gated by a first-party OAuth 2.1 login form
  (`app/oauth_provider.py`), checked against `MCP_AUTH_USERNAME`/
  `MCP_AUTH_PASSWORD` — not by client_id/redirect_uri matching, which this
  server deliberately doesn't enforce strictly for auto-registered clients
  (see `_FlexibleClientInformation`'s docstring for why that trade-off was
  made). A bug in the login-form gate itself, or in access-token
  verification, is high severity.
- `delete_item_permanently` and `move_item_to_different_library` are
  explicitly destructive: they change or remove an item's Zotero *key*,
  which silently breaks any Word document citing that item via the Zotero
  Word plugin's live field codes (see README's "Key safety" section). A
  bug that caused either of these to run without the caller's intent, or
  without version-checked concurrency, would be treated as a security
  issue, not just a correctness bug.
- Zotero and OAuth-login credentials are supplied via environment
  variables (`.env` locally, platform/CI secrets in a deployment) and are
  never committed; see
  [`CONTRIBUTING.md`](CONTRIBUTING.md#data-and-secrets).
