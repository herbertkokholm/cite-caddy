"""Settings for Cite Caddy, sourced from environment variables (see
.env.example) -- never hardcoded, since this server must work against
whichever Zotero library its deployment is pointed at (see README's
"Configuration" section).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_dotenv(path: str | None = None) -> None:
    """Populates os.environ from a .env file, without overriding variables
    already set in the environment. No-op if the file doesn't exist -- a
    real deployment sets env vars directly and has no .env file at all.
    """
    path = path or os.path.join(ROOT, ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip()
            if key and key not in os.environ:
                os.environ[key] = value


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True)
class Settings:
    library_id: str
    library_type: str
    api_key: str

    @classmethod
    def from_env(cls) -> Settings:
        library_id = os.environ.get("ZOTERO_LIBRARY_ID")
        library_type = os.environ.get("ZOTERO_LIBRARY_TYPE", "user")
        # ZOTERO_APP_KEY is accepted as an alias -- that's the label Zotero's
        # own "Applications" settings page uses for a newly created key.
        api_key = os.environ.get("ZOTERO_API_KEY") or os.environ.get("ZOTERO_APP_KEY")

        if not library_id:
            raise ConfigError("ZOTERO_LIBRARY_ID must be set (see .env.example)")
        if library_type not in ("user", "group"):
            raise ConfigError(
                f'ZOTERO_LIBRARY_TYPE must be "user" or "group", got {library_type!r}'
            )
        if not api_key:
            raise ConfigError(
                "ZOTERO_API_KEY (or ZOTERO_APP_KEY) must be set (see .env.example) "
                "-- needs write permission for this project, unlike a read-only key"
            )

        return cls(library_id=library_id, library_type=library_type, api_key=api_key)


@dataclass(frozen=True)
class HttpSettings:
    """Server-level config for HTTP mode (see app/oauth_provider.py,
    app/oauth_store.py) -- distinct from the Zotero API credentials
    above, which in HTTP mode are supplied per-tenant at login instead of
    via env vars. Only meaningful when serving over HTTP ($PORT set); a
    local stdio server has no network exposure to gate.
    """

    public_url: str
    data_dir: str
    token_store_key: str

    @classmethod
    def from_env(cls) -> HttpSettings:
        public_url = os.environ.get("MCP_PUBLIC_URL")
        data_dir = os.environ.get("MCP_DATA_DIR", os.path.join(ROOT, ".data"))
        token_store_key = os.environ.get("MCP_TOKEN_STORE_KEY")

        if not public_url:
            raise ConfigError(
                "MCP_PUBLIC_URL must be set (see .env.example) -- the public "
                "HTTPS URL this server is reachable at, e.g. "
                "https://your-domain.example (needed as the OAuth "
                "issuer/resource URL, which can't be inferred from inside "
                "the container)"
            )
        if not token_store_key:
            raise ConfigError(
                "MCP_TOKEN_STORE_KEY must be set (see .env.example) -- a "
                "Fernet key used to encrypt tenants' Zotero API keys at "
                "rest. Generate one once at deploy time with: python -c "
                '"from cryptography.fernet import Fernet; '
                'print(Fernet.generate_key().decode())"'
            )

        return cls(
            public_url=public_url.rstrip("/"),
            data_dir=data_dir,
            token_store_key=token_store_key,
        )
