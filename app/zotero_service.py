"""All Zotero read/write logic, wrapped around pyzotero.

Kept separate from app/mcp_server.py so it can be unit-tested against a fake
Zotero client (tests/fakes.py) with no network calls and no dependency on
the `mcp` package -- see README's "Testing" section: real write calls
against a live library are never exercised in tests, even when a real
ZOTERO_API_KEY is present in the environment.

Concurrency: Zotero's Web API supports optimistic concurrency via the
item's `version` field and the `If-Unmodified-Since-Version` header --
pyzotero's write methods send that header from a `version` key on the
payload you give them. Every mutating method here takes an explicit
`version` argument from the *caller* (the MCP client, which should have
gotten it from a prior get_item/search_items call) and threads it straight
through, rather than silently re-fetching the item's current version right
before writing. Re-fetching would make the check nearly meaningless -- it
would only catch a change in the few milliseconds between our own read and
write, not a change made any time after the caller last looked at the item
(e.g. an edit from the Zotero desktop app while the caller was still
deciding what to do). Using the caller's version means a real concurrent
edit is reported as a conflict (VersionConflictError) instead of silently
overwritten. See README's "Key safety" section for why that matters here in
particular: delete/move on a citation-bearing item breaks the Word document
silently, so a spurious clobber is worse than usual.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pyzotero import zotero, zotero_errors

from app.config import Settings

# Fields every mutating call must reach through a dedicated tool for
# (tags, collections, itemType, key/version bookkeeping), never through the
# free-form `fields` dict on update_item -- see update_item's docstring.
# `creators` is deliberately NOT reserved: unlike tags/collections there's
# no incremental add/remove tool for it, so a full-array replace through
# `fields` is the only way to edit an existing item's author list.
_RESERVED_UPDATE_FIELDS = {"key", "version", "itemType", "tags", "collections"}


class ZoteroServiceError(RuntimeError):
    """Base class for all errors this module raises."""


class ItemNotFoundError(ZoteroServiceError):
    pass


class CollectionNotFoundError(ZoteroServiceError):
    pass


class VersionConflictError(ZoteroServiceError):
    """The version passed by the caller no longer matches the item's
    current version on the server -- someone else (e.g. the Zotero desktop
    app) modified it since the caller last read it. Refuses the write
    rather than silently overwriting the concurrent change."""


class ValidationError(ZoteroServiceError):
    """The requested field/item-type combination is invalid per Zotero's
    schema, or the caller's arguments are otherwise malformed."""


class ZoteroAuthError(ZoteroServiceError):
    """The configured ZOTERO_API_KEY was rejected or lacks permission."""


class ZoteroApiError(ZoteroServiceError):
    """Catch-all for other Zotero API failures (rate limiting, transient
    HTTP errors, etc.)."""


def _translate(exc: Exception, *, key: str | None = None) -> ZoteroServiceError:
    if isinstance(exc, zotero_errors.ResourceNotFoundError):
        return ItemNotFoundError(f"No item found for key {key!r}" if key else str(exc))
    if isinstance(
        exc,
        (
            zotero_errors.PreConditionFailedError,
            zotero_errors.PreConditionRequiredError,
        ),
    ):
        return VersionConflictError(
            f"Version mismatch on {key!r} -- the item was modified since the "
            "version you passed was read (e.g. edited in the Zotero desktop "
            "app). Re-fetch the item to see the current state and version "
            "before retrying."
            if key
            else str(exc)
        )
    if isinstance(exc, zotero_errors.InvalidItemFieldsError):
        return ValidationError(str(exc))
    if isinstance(exc, zotero_errors.UserNotAuthorisedError):
        return ZoteroAuthError(
            "Zotero rejected the request -- check ZOTERO_API_KEY has write "
            f"permission for this library ({exc})"
        )
    if isinstance(exc, zotero_errors.PyZoteroError):
        return ZoteroApiError(str(exc))
    return ZoteroApiError(str(exc))


def _creator_name(creator: dict) -> str:
    if creator.get("name"):
        return creator["name"]
    return " ".join(p for p in (creator.get("firstName"), creator.get("lastName")) if p)


def format_creators(creators: list | None) -> str:
    return ", ".join(n for n in (_creator_name(c) for c in creators or []) if n)


def _item_summary(item: dict) -> dict:
    """Reduces a raw Zotero item (as returned by pyzotero) to the fields a
    caller needs to read, decide, and pass `version` back into a follow-up
    write call."""
    data = item.get("data", {})
    return {
        "key": data.get("key") or item.get("key"),
        "version": data.get("version", item.get("version")),
        "item_type": data.get("itemType", ""),
        "title": data.get("title", ""),
        "creators": format_creators(data.get("creators")),
        "date": data.get("date", ""),
        "doi": data.get("DOI") or None,
        "url": data.get("url") or None,
        "tags": [t["tag"] for t in data.get("tags", []) if t.get("tag")],
        "collections": data.get("collections", []),
    }


@dataclass
class ZoteroService:
    zot: zotero.Zotero
    api_key: str

    @classmethod
    def from_settings(cls, settings: Settings) -> ZoteroService:
        return cls(
            zotero.Zotero(settings.library_id, settings.library_type, settings.api_key),
            api_key=settings.api_key,
        )

    # ---- read ------------------------------------------------------

    def search_items(
        self,
        query: str = "",
        item_type: str | None = None,
        tag: str | None = None,
        collection_key: str | None = None,
        limit: int = 25,
        start: int = 0,
    ) -> list[dict]:
        kwargs: dict[str, Any] = {"limit": limit, "start": start}
        if query:
            kwargs["q"] = query
        if item_type:
            kwargs["itemType"] = item_type
        if tag:
            kwargs["tag"] = tag
        try:
            if collection_key:
                raw_items = self.zot.collection_items(collection_key, **kwargs)
            else:
                raw_items = self.zot.items(**kwargs)
        except Exception as exc:
            raise _translate(exc) from exc
        return [_item_summary(item) for item in raw_items]

    def get_item(self, key: str) -> dict:
        try:
            item = self.zot.item(key)
        except Exception as exc:
            raise _translate(exc, key=key) from exc
        return _item_summary(item)

    def list_collections(self) -> list[dict]:
        try:
            raw = self.zot.collections()
        except Exception as exc:
            raise _translate(exc) from exc
        return [
            {
                "key": c["data"]["key"],
                "version": c["data"]["version"],
                "name": c["data"]["name"],
                "parent_collection": c["data"].get("parentCollection") or None,
            }
            for c in raw
        ]

    # ---- create ------------------------------------------------------

    def create_item(
        self,
        item_type: str,
        fields: dict[str, Any] | None = None,
        creators: list[dict[str, str]] | None = None,
        tags: list[str] | None = None,
        collections: list[str] | None = None,
    ) -> dict:
        try:
            template = self.zot.item_template(item_type)
        except Exception as exc:
            raise _translate(exc) from exc

        template.update(fields or {})
        if creators is not None:
            template["creators"] = creators
        if tags is not None:
            template["tags"] = [{"tag": t} for t in tags]
        if collections is not None:
            template["collections"] = collections

        try:
            result = self.zot.create_items([template])
        except Exception as exc:
            raise _translate(exc) from exc

        failed = result.get("failed") or {}
        if failed:
            raise ValidationError(f"Zotero rejected the new item: {failed}")
        success = result.get("successful") or result.get("success") or {}
        created = next(iter(success.values()))
        # `create_items` (>=1.5 API) returns the full created item under
        # "successful"; older/mocked responses may return just the key
        # under "success" -- normalize both to an _item_summary shape.
        if isinstance(created, dict):
            return _item_summary(created)
        return self.get_item(created)

    # ---- update (key-preserving) --------------------------------------

    def update_item(self, key: str, version: int, fields: dict[str, Any]) -> dict:
        """Patches bibliographic fields (title, date, DOI, url,
        abstractNote, publicationTitle, creators, etc.) on an existing
        item, in place -- the item's key is unchanged, so any Word
        citation referencing it keeps working. `creators` (if passed)
        replaces the whole author/editor list, not merges into it.

        `fields` may not contain tags/collections/itemType/key/version --
        use add_tags/remove_tags/set_tags for tags and
        add_to_collection/remove_from_collection for collection membership,
        so a client can reason about "this is a safe field edit" vs. "this
        touches tags/collections" from the tool name alone.
        """
        reserved = _RESERVED_UPDATE_FIELDS & fields.keys()
        if reserved:
            raise ValidationError(
                f"fields may not include {sorted(reserved)} -- use the dedicated "
                "tag/collection tools for those"
            )
        return self._patch(key, version, dict(fields))

    def _patch(self, key: str, version: int, data: dict[str, Any]) -> dict:
        payload = {"key": key, "version": version, **data}
        try:
            self.zot.update_item(payload)
        except Exception as exc:
            raise _translate(exc, key=key) from exc
        return self.get_item(key)

    # ---- tags (key-preserving) -----------------------------------------

    def add_tags(self, key: str, version: int, tags: list[str]) -> dict:
        current = self._current_data(key)
        merged = sorted({t["tag"] for t in current.get("tags", [])} | set(tags))
        return self._patch(key, version, {"tags": [{"tag": t} for t in merged]})

    def remove_tags(self, key: str, version: int, tags: list[str]) -> dict:
        current = self._current_data(key)
        remove = set(tags)
        kept = [t for t in current.get("tags", []) if t.get("tag") not in remove]
        return self._patch(key, version, {"tags": kept})

    def set_tags(self, key: str, version: int, tags: list[str]) -> dict:
        return self._patch(key, version, {"tags": [{"tag": t} for t in tags]})

    def _current_data(self, key: str) -> dict:
        try:
            return self.zot.item(key)["data"]
        except Exception as exc:
            raise _translate(exc, key=key) from exc

    # ---- collection membership (key-preserving "move") -----------------

    def add_to_collection(self, key: str, version: int, collection_key: str) -> dict:
        """Files an item into a collection, in addition to any it's
        already in. Safe: the item's key is unchanged, unlike
        move_item_to_library."""
        current = self._current_data(key)
        collections = set(current.get("collections", [])) | {collection_key}
        return self._patch(key, version, {"collections": sorted(collections)})

    def remove_from_collection(
        self, key: str, version: int, collection_key: str
    ) -> dict:
        """Removes an item from one collection; it stays in the library
        and any other collections it's filed under. Safe: the item's key
        is unchanged."""
        current = self._current_data(key)
        collections = set(current.get("collections", [])) - {collection_key}
        return self._patch(key, version, {"collections": sorted(collections)})

    def create_collection(self, name: str, parent_key: str | None = None) -> dict:
        payload = [{"name": name, "parentCollection": parent_key or False}]
        try:
            result = self.zot.create_collections(payload)
        except Exception as exc:
            raise _translate(exc) from exc
        failed = result.get("failed") or {}
        if failed:
            raise ValidationError(f"Zotero rejected the new collection: {failed}")
        success = result.get("success") or result.get("successful") or {}
        created_key = next(iter(success.values()))
        if isinstance(created_key, dict):
            created_key = created_key.get("key")
        return {
            "key": created_key,
            "name": name,
            "parent_collection": parent_key or None,
        }

    # ---- destructive: delete --------------------------------------------

    def delete_item(self, key: str, version: int) -> dict:
        """DESTRUCTIVE. Permanently deletes the item from its library.

        Any Word document citing this item via the Zotero Word plugin's
        live field code references it by this key -- once deleted, that
        citation is broken silently; the document won't error until
        someone next updates fields or refreshes the citation. There is no
        confirmation step at this layer; the caller (the MCP client / its
        model) is responsible for making sure this is really what's wanted
        before calling it. `version` must match the item's current version
        or the call is refused (VersionConflictError) rather than deleting
        a version the caller hasn't actually seen.
        """
        try:
            self.zot.delete_item({"key": key, "version": version})
        except Exception as exc:
            raise _translate(exc, key=key) from exc
        return {"key": key, "deleted": True}

    # ---- destructive: cross-library move --------------------------------

    def move_item_to_library(
        self,
        key: str,
        version: int,
        target_library_id: str,
        target_library_type: str,
    ) -> dict:
        """DESTRUCTIVE. Zotero has no native cross-library move -- this
        recreates the item in the target library, then deletes it from the
        source. The recreated item gets a brand-new key.

        Any Word citation referencing the *original* key is broken by this
        operation, exactly like delete_item -- silently, and permanently.
        Moving an item between *collections in the same library* (see
        add_to_collection/remove_from_collection) does not have this
        problem and should be preferred whenever the goal is just
        reorganizing, not actually relocating to a different library.

        The configured ZOTERO_API_KEY must have write access to
        target_library_id as well as the source library.
        """
        try:
            current = self.zot.item(key)
        except Exception as exc:
            raise _translate(exc, key=key) from exc

        data = dict(current.get("data", {}))
        if data.get("version") != version:
            # Fail before creating anything in the target library -- a
            # stale version here is usually just the caller not having
            # re-fetched, not a genuine race; catching it now avoids
            # leaving an orphaned copy in the target library that a
            # post-create version check would otherwise create.
            raise _translate(zotero_errors.PreConditionFailedError(), key=key)
        item_type = data.get("itemType")
        for field in (
            "key",
            "version",
            "dateAdded",
            "dateModified",
            "collections",
            "relations",
        ):
            data.pop(field, None)

        try:
            target_zot = zotero.Zotero(
                target_library_id, target_library_type, self.api_key
            )
            result = target_zot.create_items([data])
        except Exception as exc:
            raise _translate(exc) from exc

        failed = result.get("failed") or {}
        if failed:
            raise ValidationError(
                f"Could not create item in target library {target_library_id} "
                f"({target_library_type}); source item was NOT deleted: {failed}"
            )
        success = result.get("successful") or result.get("success") or {}
        created = next(iter(success.values()))
        new_key = created.get("key") if isinstance(created, dict) else created

        try:
            self.zot.delete_item({"key": key, "version": version})
        except Exception as exc:
            raise _translate(
                ZoteroApiError(
                    f"Item was created in the target library as {new_key!r}, but "
                    f"deleting it from the source library failed: {exc}. The item "
                    f"now exists in BOTH libraries -- delete the source copy "
                    f"manually once you've confirmed the target copy is correct."
                ),
                key=key,
            ) from exc

        return {
            "old_key": key,
            "new_key": new_key,
            "item_type": item_type,
            "target_library_id": target_library_id,
            "target_library_type": target_library_type,
            "deleted_from_source": True,
            "warning": (
                f"Item moved to key {new_key!r} in library {target_library_id}. "
                f"Any Word citation referencing the old key {key!r} is now broken."
            ),
        }
