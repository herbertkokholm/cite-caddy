"""Persistent storage for app/oauth_provider.py's OAuth state (registered
clients, issued authorization codes, access/refresh tokens).

A plain JSON file, not a database -- this server has exactly one resource
owner and, realistically, a handful of registered clients (one per MCP
client app that's ever connected), so a file is simpler than standing up
real storage and is easy to inspect/wipe by hand if something goes wrong.
Persisted (rather than in-memory only) so that redeploying the container
doesn't silently log every connected MCP client out.
"""

from __future__ import annotations

import json
import os
import threading
from typing import Any


class TokenStore:
    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._data: dict[str, dict[str, Any]] = {
            "clients": {},
            "auth_codes": {},
            "access_tokens": {},
            "refresh_tokens": {},
        }
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self._path):
            return
        with open(self._path, encoding="utf-8") as f:
            self._data = json.load(f)

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
