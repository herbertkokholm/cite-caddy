"""Unit tests for app/metrics.py's counters -- app/mcp_server.py's `/status`
wiring (middleware + endpoint) is covered separately in
test_mcp_server_oauth.py, against the real HTTP app.
"""

import time

from app import metrics


def test_uptime_seconds_counts_up_from_import_time(monkeypatch):
    monkeypatch.setattr(metrics, "START_TIME", time.time() - 5)
    assert metrics.uptime_seconds() >= 5


def test_tool_calls_and_errors_are_independent_counters():
    metrics.reset()

    metrics.tool_calls["search_items"] += 1
    metrics.tool_calls["search_items"] += 1
    metrics.tool_errors["search_items"] += 1

    assert metrics.tool_calls["search_items"] == 2
    assert metrics.tool_errors["search_items"] == 1
    assert metrics.tool_calls["add_tags"] == 0
