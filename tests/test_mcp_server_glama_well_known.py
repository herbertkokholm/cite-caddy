"""Tests app/mcp_server.py's /.well-known/glama.json route: Glama.ai's
Connector listing (glama.ai/mcp/connectors/...) requires this path to be
served at the live URL to verify server ownership -- confirmed missing by
a 404 in production before this route existed. This must return exactly
the repo-root glama.json's content, since Glama's separate, repo-based
"Servers" grading (glama.ai/mcp/servers/...) already reads that same file
via GitHub -- serving anything else here would let the two drift apart.
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from starlette.testclient import TestClient


@pytest.fixture
def http_mcp_server(tmp_path, monkeypatch):
    monkeypatch.setenv("PORT", "8299")
    monkeypatch.delenv("ZOTERO_LIBRARY_ID", raising=False)
    monkeypatch.delenv("ZOTERO_API_KEY", raising=False)
    monkeypatch.setenv("MCP_TOKEN_STORE_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("MCP_PUBLIC_URL", "https://example.test")
    monkeypatch.setenv("MCP_DATA_DIR", str(tmp_path))

    sys.modules.pop("app.mcp_server", None)
    module = importlib.import_module("app.mcp_server")
    yield module
    sys.modules.pop("app.mcp_server", None)


@pytest.fixture
def client(http_mcp_server) -> TestClient:
    with TestClient(http_mcp_server.mcp.streamable_http_app()) as test_client:
        yield test_client


def test_glama_well_known_matches_repo_root_file(client: TestClient):
    resp = client.get("/.well-known/glama.json")

    assert resp.status_code == 200
    repo_root = Path(__file__).resolve().parent.parent
    expected = json.loads((repo_root / "glama.json").read_text())
    assert resp.json() == expected


def test_glama_well_known_is_unauthenticated(client: TestClient):
    # No Authorization header at all -- this must not 401 like /mcp does.
    resp = client.get("/.well-known/glama.json")
    assert resp.status_code != 401
