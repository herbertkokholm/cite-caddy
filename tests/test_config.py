import pytest

from app.config import ConfigError, HttpSettings, Settings


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


# ---- HttpSettings ----------------------------------------------------------


def _clear_http_env(monkeypatch):
    for var in ("MCP_PUBLIC_URL", "MCP_DATA_DIR", "MCP_TOKEN_STORE_KEY"):
        monkeypatch.delenv(var, raising=False)


def test_http_settings_requires_public_url(monkeypatch):
    _clear_http_env(monkeypatch)
    monkeypatch.setenv("MCP_TOKEN_STORE_KEY", "fake-fernet-key")
    with pytest.raises(ConfigError, match="MCP_PUBLIC_URL"):
        HttpSettings.from_env()


def test_http_settings_requires_token_store_key(monkeypatch):
    _clear_http_env(monkeypatch)
    monkeypatch.setenv("MCP_PUBLIC_URL", "https://example.test")
    with pytest.raises(ConfigError, match="MCP_TOKEN_STORE_KEY"):
        HttpSettings.from_env()


def test_http_settings_strips_trailing_slash_from_public_url(monkeypatch):
    _clear_http_env(monkeypatch)
    monkeypatch.setenv("MCP_PUBLIC_URL", "https://example.test/")
    monkeypatch.setenv("MCP_TOKEN_STORE_KEY", "fake-fernet-key")
    settings = HttpSettings.from_env()
    assert settings.public_url == "https://example.test"


def test_http_settings_defaults_data_dir(monkeypatch):
    _clear_http_env(monkeypatch)
    monkeypatch.setenv("MCP_PUBLIC_URL", "https://example.test")
    monkeypatch.setenv("MCP_TOKEN_STORE_KEY", "fake-fernet-key")
    settings = HttpSettings.from_env()
    assert settings.data_dir.endswith(".data")
