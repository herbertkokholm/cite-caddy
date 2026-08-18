"""Tests app/mcp_server.py's /llms.txt route: serves the repo-root
llms.txt (the llmstxt.org convention -- a robots.txt-style, LLM-readable
summary + links file) at this server's own site root, unauthenticated,
matching the convention's expected location.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from starlette.testclient import TestClient


@pytest.fixture
def http_mcp_server(tmp_path, monkeypatch):
    monkeypatch.setenv("PORT", "8399")
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


def test_llms_txt_matches_repo_root_file(client: TestClient):
    resp = client.get("/llms.txt")

    assert resp.status_code == 200
    repo_root = Path(__file__).resolve().parent.parent
    assert resp.text == (repo_root / "llms.txt").read_text()


def test_llms_txt_is_unauthenticated(client: TestClient):
    resp = client.get("/llms.txt")
    assert resp.status_code != 401
