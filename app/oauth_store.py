"""Persistent storage for app/oauth_provider.py's OAuth state (registered
clients, issued authorization codes, access/refresh tokens) and for
onboarded tenants' Zotero credentials.

A plain JSON file, not a database -- realistically a handful of
registered clients (one per MCP client app that's ever connected) and
tenants (one per onboarded Zotero library), so a file is simpler than
standing up real storage and is easy to inspect/wipe by hand if something
goes wrong. Persisted (rather than in-memory only) so that redeploying
the container doesn't silently log every connected MCP client out or
forget every onboarded tenant.

Each tenant's Zotero API key is encrypted at rest with Fernet (symmetric,
authenticated encryption) using MCP_TOKEN_STORE_KEY -- the rest of a
tenant record (library_id, library_type) stays plaintext, same as the
rest of this file's contents.
"""

from __future__ import annotations

import json
import os
import threading
from typing import Any

from cryptography.fernet import Fernet


class TokenStore:
    def __init__(self, path: str, fernet_key: str) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._fernet = Fernet(fernet_key.encode())
        self._data: dict[str, dict[str, Any]] = {
            "clients": {},
            "auth_codes": {},
            "access_tokens": {},
            "refresh_tokens": {},
            "tenants": {},
        }
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self._path):
            return
        with open(self._path, encoding="utf-8") as f:
            loaded = json.load(f)
        # Backfill any collection missing from an older store file (e.g.
        # "tenants", added after some stores were already deployed)
        # instead of clobbering it -- existing clients/tokens must survive
        # an upgrade.
        for kind, default in self._data.items():
            loaded.setdefault(kind, default)
        self._data = loaded

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        tmp_path = self._path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(self._data, f)
        os.replace(tmp_path, self._path)

    def get(self, kind: str, key: str) -> dict[str, Any] | None:
        with self._lock:
            return self._data[kind].get(key)

    def put(self, kind: str, key: str, value: dict[str, Any]) -> None:
        with self._lock:
            self._data[kind][key] = value
            self._save()

    def delete(self, kind: str, key: str) -> None:
        with self._lock:
            if key in self._data[kind]:
                del self._data[kind][key]
                self._save()

    # ---- tenants (Zotero API key encrypted at rest) ---------------------

    def get_tenant(self, library_id: str) -> dict[str, Any] | None:
        data = self.get("tenants", library_id)
        if data is None:
            return None
        tenant = dict(data)
        tenant["api_key"] = self._fernet.decrypt(tenant["api_key"].encode()).decode()
        return tenant

    def put_tenant(self, library_id: str, tenant: dict[str, Any]) -> None:
        # A natural home for a future `plan` field (billing/quotas), but
        # that's a deliberately separate follow-on piece -- not built here.
        encrypted = dict(tenant)
        encrypted["api_key"] = self._fernet.encrypt(tenant["api_key"].encode()).decode()
        self.put("tenants", library_id, encrypted)
