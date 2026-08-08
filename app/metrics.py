"""In-process, aggregate counters backing app/mcp_server.py's `/status`
endpoint: process uptime plus per-tool call/error counts.

Deliberately in-memory only (reset on restart/redeploy, and never
persisted) -- this is a lightweight "is it up and roughly how busy is
it" signal, not a metrics/analytics system. Deliberately has no
per-tenant or per-Zotero-library breakdown, only per-tool-name, so
`/status` can stay unauthenticated without leaking who is using the
server or how much: see README's "Monitoring" section.
"""

from __future__ import annotations

import time
from collections import Counter

START_TIME = time.time()
tool_calls: Counter[str] = Counter()
tool_errors: Counter[str] = Counter()


def uptime_seconds() -> int:
    return int(time.time() - START_TIME)


def reset() -> None:
    """Test-only: clears both counters. Only needed by tests -- the
    counters otherwise live for the whole process lifetime, and
    `sys.modules` tricks don't actually get a fresh module here (the
    `app` package keeps its own `metrics` attribute alive across a pop/
    reimport of `app.mcp_server`), so tests reset explicitly instead."""
    tool_calls.clear()
    tool_errors.clear()
