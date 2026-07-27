"""An in-memory fake of the subset of pyzotero.zotero.Zotero's interface
that app/zotero_service.py calls, so ZoteroService (and the MCP tools built
on it) can be exercised in tests with no network access and no live Zotero
library involved -- see README's "Testing" note: real write calls are never
made in tests, even with real ZOTERO_API_KEY credentials in the environment.

Mirrors the behaviors ZoteroService actually depends on: version-checked
writes (If-Unmodified-Since-Version -> PreConditionFailedError on
mismatch), 404s for unknown keys, and the create_items/create_collections
success/failed response shape.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any

from pyzotero import zotero_errors


class FakeZotero:
    def __init__(self, library_id: str = "123", library_type: str = "user") -> None:
        self.library_id = library_id
        self.library_type = library_type
        self._items: dict[str, dict[str, Any]] = {}
        self._collections: dict[str, dict[str, Any]] = {}
        self._files: dict[str, bytes] = {}
        self._fulltext: dict[str, dict[str, Any]] = {}
        self._next_id = 1

    # ---- test setup helpers (not part of pyzotero's real interface) ----

    def seed_item(self, item_type: str, data: dict[str, Any] | None = None) -> dict:
        key = self._new_key()
        full = {
            "key": key,
            "version": 1,
            "library": {"type": self.library_type, "id": self.library_id},
            "links": {},
            "meta": {},
            "data": {
                "key": key,
                "version": 1,
                "itemType": item_type,
                "title": "",
                "creators": [],
                "tags": [],
                "collections": [],
                **(data or {}),
            },
        }
        self._items[key] = full
        return full

    def seed_collection(self, name: str, parent_key: str | None = None) -> dict:
        key = self._new_key()
        full = {
            "key": key,
            "version": 1,
            "data": {
                "key": key,
                "version": 1,
                "name": name,
                "parentCollection": parent_key or False,
            },
        }
        self._collections[key] = full
        return full

    def seed_attachment(
        self,
        parent_key: str,
        *,
        title: str = "paper.pdf",
        filename: str = "paper.pdf",
        content: bytes = b"%PDF-fake",
        content_type: str = "application/pdf",
    ) -> dict:
        key = self._new_key()
        full = {
            "key": key,
            "version": 1,
            "library": {"type": self.library_type, "id": self.library_id},
            "links": {},
            "meta": {},
            "data": {
                "key": key,
                "version": 1,
                "itemType": "attachment",
                "linkMode": "imported_file",
                "title": title,
                "filename": filename,
                "contentType": content_type,
                "parentItem": parent_key,
                "tags": [],
            },
        }
        self._items[key] = full
        self._files[key] = content
        return full

    def seed_fulltext(self, key: str, content: str, **extra: Any) -> None:
        self._fulltext[key] = {"content": content, **extra}

    def seed_note(
        self, parent_key: str, content: str = "<p>A note.</p>", **extra: Any
    ) -> dict:
        key = self._new_key()
        full = {
            "key": key,
            "version": 1,
            "library": {"type": self.library_type, "id": self.library_id},
            "links": {},
            "meta": {},
            "data": {
                "key": key,
                "version": 1,
                "itemType": "note",
                "note": content,
                "parentItem": parent_key,
                "tags": [],
                **extra,
            },
        }
        self._items[key] = full
        return full

    def _new_key(self) -> str:
        key = f"KEY{self._next_id}"
        self._next_id += 1
        return key

    # ---- pyzotero-shaped interface used by ZoteroService ----

    def item_template(self, itemtype: str, linkmode: str | None = None) -> dict:
        return {
            "itemType": itemtype,
            "title": "",
            "creators": [],
            "tags": [],
            "collections": [],
        }

    def create_items(
        self, payload: list[dict[str, Any]], parentid: str | None = None, **_: Any
    ) -> dict:
        if parentid is not None and parentid not in self._items:
            raise zotero_errors.ResourceNotFoundError(f"No item {parentid}")
        success: dict[str, str] = {}
        successful: dict[str, dict] = {}
        for i, data in enumerate(payload):
            key = self._new_key()
            item_data = {**data, "key": key, "version": 1}
            if parentid:
                item_data["parentItem"] = parentid
            full = {
                "key": key,
                "version": 1,
                "library": {"type": self.library_type, "id": self.library_id},
                "links": {},
                "meta": {},
                "data": item_data,
            }
            self._items[key] = full
            success[str(i)] = key
            successful[str(i)] = full
        return {
            "success": success,
            "successful": successful,
            "unchanged": {},
            "failed": {},
        }

    def item(self, key: str, **kwargs: Any) -> dict:
        try:
            return self._items[key]
        except KeyError:
            raise zotero_errors.ResourceNotFoundError(f"No item {key}") from None

    def items(self, **kwargs: Any) -> list[dict]:
        results = list(self._items.values())
        q = kwargs.get("q")
        if q:
            results = [
                i
                for i in results
                if q.lower() in (i["data"].get("title") or "").lower()
            ]
        item_type = kwargs.get("itemType")
        if item_type:
            results = [i for i in results if i["data"].get("itemType") == item_type]
        tag = kwargs.get("tag")
        if tag:
            results = [
                i
                for i in results
                if tag in [t.get("tag") for t in i["data"].get("tags", [])]
            ]
        start = kwargs.get("start", 0)
        limit = kwargs.get("limit")
        if limit is not None:
            return results[start : start + limit]
        return results[start:]

    def collection_items(self, collection: str, **kwargs: Any) -> list[dict]:
        results = [
            i
            for i in self._items.values()
            if collection in i["data"].get("collections", [])
        ]
        limit = kwargs.get("limit")
        start = kwargs.get("start", 0)
        if limit is not None:
            return results[start : start + limit]
        return results[start:]

    def update_item(
        self, payload: dict[str, Any], last_modified: int | None = None
    ) -> None:
        key = payload["key"]
        if key not in self._items:
            raise zotero_errors.ResourceNotFoundError(f"No item {key}")
        current = self._items[key]
        expected_version = (
            last_modified if last_modified is not None else payload["version"]
        )
        if current["data"]["version"] != expected_version:
            raise zotero_errors.PreConditionFailedError(
                f"Version mismatch for {key}: expected {current['data']['version']}, "
                f"got {expected_version}"
            )
        changes = {k: v for k, v in payload.items() if k not in ("key", "version")}
        current["data"].update(changes)
        current["data"]["version"] += 1
        current["version"] = current["data"]["version"]

    def delete_item(
        self, payload: dict[str, Any], last_modified: int | None = None
    ) -> None:
        key = payload["key"]
        if key not in self._items:
            raise zotero_errors.ResourceNotFoundError(f"No item {key}")
        current = self._items[key]
        expected_version = (
            last_modified if last_modified is not None else payload["version"]
        )
        if current["data"]["version"] != expected_version:
            raise zotero_errors.PreConditionFailedError(
                f"Version mismatch for {key}: expected {current['data']['version']}, "
                f"got {expected_version}"
            )
        del self._items[key]

    def children(self, item: str, **kwargs: Any) -> list[dict]:
        if item not in self._items:
            raise zotero_errors.ResourceNotFoundError(f"No item {item}")
        return [i for i in self._items.values() if i["data"].get("parentItem") == item]

    def fulltext_item(self, itemkey: str, **kwargs: Any) -> dict:
        try:
            return self._fulltext[itemkey]
        except KeyError:
            raise zotero_errors.ResourceNotFoundError(
                f"No fulltext for {itemkey}"
            ) from None

    def file(self, item: str, **kwargs: Any) -> bytes:
        try:
            return self._files[item]
        except KeyError:
            raise zotero_errors.ResourceNotFoundError(f"No file for {item}") from None

    def attachment_both(
        self, files: list[tuple[str, str]], parentid: str | None = None
    ) -> dict:
        if parentid is not None and parentid not in self._items:
            raise zotero_errors.ResourceNotFoundError(f"No item {parentid}")
        success = []
        for title, filepath in files:
            key = self._new_key()
            content = Path(filepath).read_bytes()
            content_type = mimetypes.guess_type(filepath)[0] or "application/octet-stream"
            data = {
                "key": key,
                "version": 1,
                "itemType": "attachment",
                "linkMode": "imported_file",
                "title": title,
                "filename": Path(filepath).name,
                "contentType": content_type,
                "tags": [],
            }
            if parentid:
                data["parentItem"] = parentid
            full = {
                "key": key,
                "version": 1,
                "library": {"type": self.library_type, "id": self.library_id},
                "links": {},
                "meta": {},
                "data": data,
            }
            self._items[key] = full
            self._files[key] = content
            success.append(dict(data))
        return {"success": success, "failure": [], "unchanged": []}

    def collections(self, **kwargs: Any) -> list[dict]:
        return list(self._collections.values())

    def collection(self, collection: str, **kwargs: Any) -> dict:
        try:
            return self._collections[collection]
        except KeyError:
            raise zotero_errors.ResourceNotFoundError(
                f"No collection {collection}"
            ) from None

    def update_collection(
        self, payload: dict[str, Any], last_modified: int | None = None
    ) -> None:
        key = payload["key"]
        if key not in self._collections:
            raise zotero_errors.ResourceNotFoundError(f"No collection {key}")
        current = self._collections[key]
        expected_version = (
            last_modified if last_modified is not None else payload["version"]
        )
        if current["data"]["version"] != expected_version:
            raise zotero_errors.PreConditionFailedError(
                f"Version mismatch for {key}: expected "
                f"{current['data']['version']}, got {expected_version}"
            )
        changes = {k: v for k, v in payload.items() if k not in ("key", "version")}
        current["data"].update(changes)
        current["data"]["version"] += 1
        current["version"] = current["data"]["version"]

    def delete_collection(
        self, payload: dict[str, Any], last_modified: int | None = None
    ) -> None:
        key = payload["key"]
        if key not in self._collections:
            raise zotero_errors.ResourceNotFoundError(f"No collection {key}")
        current = self._collections[key]
        expected_version = (
            last_modified if last_modified is not None else payload["version"]
        )
        if current["data"]["version"] != expected_version:
            raise zotero_errors.PreConditionFailedError(
                f"Version mismatch for {key}: expected "
                f"{current['data']['version']}, got {expected_version}"
            )
        del self._collections[key]

    def create_collections(self, payload: list[dict[str, Any]], **_: Any) -> dict:
        success: dict[str, str] = {}
        for i, item in enumerate(payload):
            key = self._new_key()
            full = {
                "key": key,
                "version": 1,
                "data": {
                    "key": key,
                    "version": 1,
                    "name": item["name"],
                    "parentCollection": item.get("parentCollection") or False,
                },
            }
            self._collections[key] = full
            success[str(i)] = key
        return {"success": success, "unchanged": {}, "failed": {}}
