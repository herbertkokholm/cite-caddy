"""Tests app/mcp_server.py's optional MCP_WEBSITE_URL wiring: serverInfo's
website_url/icons must be omitted entirely (not a broken "None..." icon
src) when the var isn't set, and built correctly from it when it is.
Reimports the module fresh under each env, same pattern as
test_mcp_server_multitenant.py's http_module fixture.
"""

from __future__ import annotations

import importlib
import sys

import pytest


@pytest.fixture
def fresh_module(monkeypatch):
    monkeypatch.delenv("PORT", raising=False)

    def _import():
        sys.modules.pop("app.mcp_server", None)
        return importlib.import_module("app.mcp_server")

    yield _import
    sys.modules.pop("app.mcp_server", None)


def test_website_url_and_icons_omitted_when_unset(fresh_module, monkeypatch):
    monkeypatch.delenv("MCP_WEBSITE_URL", raising=False)
    module = fresh_module()

    assert module.mcp._mcp_server.website_url is None
    assert module.mcp._mcp_server.icons is None


def test_website_url_and_icons_set_when_configured(fresh_module, monkeypatch):
    monkeypatch.setenv("MCP_WEBSITE_URL", "https://example.test/cite-caddy/")
    module = fresh_module()

    assert module.mcp._mcp_server.website_url == "https://example.test/cite-caddy/"
    icons = module.mcp._mcp_server.icons
    assert icons is not None
    assert icons[0].src == "https://example.test/cite-caddy/icons/icon.svg"
    assert icons[0].mimeType == "image/svg+xml"


def test_reports_own_package_version_not_mcp_sdk_version(fresh_module, monkeypatch):
    from importlib.metadata import version as pkg_version

    module = fresh_module()

    assert module.mcp._mcp_server.version == pkg_version("cite-caddy")
