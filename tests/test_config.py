import pytest

from app.config import AuthSettings, ConfigError, Settings


def test_from_env_requires_library_id(monkeypatch):
    monkeypatch.delenv("ZOTERO_LIBRARY_ID", raising=False)
    monkeypatch.setenv("ZOTERO_API_KEY", "key")
    with pytest.raises(ConfigError, match="ZOTERO_LIBRARY_ID"):
        Settings.from_env()


def test_from_env_requires_api_key(monkeypatch):
    monkeypatch.setenv("ZOTERO_LIBRARY_ID", "123")
    monkeypatch.delenv("ZOTERO_API_KEY", raising=False)
    monkeypatch.delenv("ZOTERO_APP_KEY", raising=False)
    with pytest.raises(ConfigError, match="ZOTERO_API_KEY"):
        Settings.from_env()


def test_from_env_rejects_bad_library_type(monkeypatch):
    monkeypatch.setenv("ZOTERO_LIBRARY_ID", "123")
    monkeypatch.setenv("ZOTERO_API_KEY", "key")
    monkeypatch.setenv("ZOTERO_LIBRARY_TYPE", "nonsense")
    with pytest.raises(ConfigError, match="ZOTERO_LIBRARY_TYPE"):
        Settings.from_env()


def test_from_env_defaults_library_type_to_user(monkeypatch):
    monkeypatch.setenv("ZOTERO_LIBRARY_ID", "123")
    monkeypatch.setenv("ZOTERO_API_KEY", "key")
    monkeypatch.delenv("ZOTERO_LIBRARY_TYPE", raising=False)
    settings = Settings.from_env()
    assert settings.library_type == "user"


def test_from_env_accepts_zotero_app_key_alias(monkeypatch):
    monkeypatch.setenv("ZOTERO_LIBRARY_ID", "123")
    monkeypatch.delenv("ZOTERO_API_KEY", raising=False)
    monkeypatch.setenv("ZOTERO_APP_KEY", "app-key-value")
    settings = Settings.from_env()
    assert settings.api_key == "app-key-value"


def test_from_env_reads_all_values(monkeypatch):
    monkeypatch.setenv("ZOTERO_LIBRARY_ID", "999")
    monkeypatch.setenv("ZOTERO_LIBRARY_TYPE", "group")
    monkeypatch.setenv("ZOTERO_API_KEY", "secret")
    settings = Settings.from_env()
    assert settings == Settings(
        library_id="999", library_type="group", api_key="secret"
    )


# ---- AuthSettings ----------------------------------------------------------


def _clear_auth_env(monkeypatch):
    for var in (
        "MCP_AUTH_USERNAME",
        "MCP_AUTH_PASSWORD",
        "MCP_PUBLIC_URL",
        "MCP_DATA_DIR",
    ):
        monkeypatch.delenv(var, raising=False)


def test_auth_settings_requires_username(monkeypatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("MCP_AUTH_PASSWORD", "pw")
    monkeypatch.setenv("MCP_PUBLIC_URL", "https://example.test")
    with pytest.raises(ConfigError, match="MCP_AUTH_USERNAME"):
        AuthSettings.from_env()


def test_auth_settings_requires_password(monkeypatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("MCP_AUTH_USERNAME", "thomas")
    monkeypatch.setenv("MCP_PUBLIC_URL", "https://example.test")
    with pytest.raises(ConfigError, match="MCP_AUTH_PASSWORD"):
        AuthSettings.from_env()


def test_auth_settings_requires_public_url(monkeypatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("MCP_AUTH_USERNAME", "thomas")
    monkeypatch.setenv("MCP_AUTH_PASSWORD", "pw")
    with pytest.raises(ConfigError, match="MCP_PUBLIC_URL"):
        AuthSettings.from_env()


def test_auth_settings_strips_trailing_slash_from_public_url(monkeypatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("MCP_AUTH_USERNAME", "thomas")
    monkeypatch.setenv("MCP_AUTH_PASSWORD", "pw")
    monkeypatch.setenv("MCP_PUBLIC_URL", "https://example.test/")
    settings = AuthSettings.from_env()
    assert settings.public_url == "https://example.test"


def test_auth_settings_defaults_data_dir(monkeypatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("MCP_AUTH_USERNAME", "thomas")
    monkeypatch.setenv("MCP_AUTH_PASSWORD", "pw")
    monkeypatch.setenv("MCP_PUBLIC_URL", "https://example.test")
    settings = AuthSettings.from_env()
    assert settings.data_dir.endswith(".data")
