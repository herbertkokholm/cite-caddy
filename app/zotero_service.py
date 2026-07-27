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

import base64
import tempfile
from dataclasses import dataclass
from pathlib import Path
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


def _translate(
    exc: Exception,
    *,
    key: str | None = None,
    not_found_cls: type[ZoteroServiceError] = ItemNotFoundError,
    noun: str = "item",
) -> ZoteroServiceError:
    if isinstance(exc, zotero_errors.ResourceNotFoundError):
        return not_found_cls(
            f"No {noun} found for key {key!r}" if key else str(exc)
        )
    if isinstance(
        exc,
        (
            zotero_errors.PreConditionFailedError,
            zotero_errors.PreConditionRequiredError,
        ),
    ):
        return VersionConflictError(
            f"Version mismatch on {key!r} -- the {noun} was modified since "
            "the version you passed was read (e.g. edited in the Zotero "
            "desktop app). Re-fetch it to see the current state and "
            "version before retrying."
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


def _attachment_summary(item: dict) -> dict:
    """Reduces a raw Zotero attachment item to the fields a caller needs
    to identify it and act on it (download, fulltext lookup). Unlike
    _item_summary, no creators/date/doi -- attachments don't carry them."""
    data = item.get("data", {})
    return {
        "key": data.get("key") or item.get("key"),
        "version": data.get("version", item.get("version")),
        "title": data.get("title", ""),
        "filename": data.get("filename", ""),
        "content_type": data.get("contentType", ""),
        "link_mode": data.get("linkMode", ""),
        "parent_item": data.get("parentItem"),
        "tags": [t["tag"] for t in data.get("tags", []) if t.get("tag")],
    }


def _collection_summary(collection: dict) -> dict:
    data = collection["data"]
    return {
        "key": data["key"],
        "version": data["version"],
        "name": data["name"],
        "parent_collection": data.get("parentCollection") or None,
    }


def _note_summary(item: dict) -> dict:
    """Reduces a raw Zotero note item to the fields a caller needs to
    read its content and write it back via update_note."""
    data = item.get("data", {})
    return {
        "key": data.get("key") or item.get("key"),
        "version": data.get("version", item.get("version")),
        "content": data.get("note", ""),
        "parent_item": data.get("parentItem"),
        "tags": [t["tag"] for t in data.get("tags", []) if t.get("tag")],
    }


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
        return [_collection_summary(c) for c in raw]

    # ---- attachments & fulltext (read) ----------------------------------

    def list_attachments(self, item_key: str) -> list[dict]:
        """List the file attachments (PDFs, snapshots, etc.) filed under
        an item -- not its notes. Read-only. Each result's `key` can be
        passed to download_attachment or get_fulltext."""
        try:
            children = self.zot.children(item_key)
        except Exception as exc:
            raise _translate(exc, key=item_key) from exc
        return [
            _attachment_summary(c)
            for c in children
            if c.get("data", {}).get("itemType") == "attachment"
        ]

    def get_fulltext(self, attachment_key: str) -> dict:
        """Fetch Zotero's extracted full-text content and indexing
        progress for an attachment. Only meaningful for attachments Zotero
        has indexed (PDFs/text files with extracted text); raises
        ItemNotFoundError if there's no indexed full text for this key.
        Read-only."""
        try:
            data = self.zot.fulltext_item(attachment_key)
        except Exception as exc:
            raise _translate(exc, key=attachment_key) from exc
        return {
            "key": attachment_key,
            "content": data.get("content", ""),
            "indexed_pages": data.get("indexedPages"),
            "total_pages": data.get("totalPages"),
            "indexed_chars": data.get("indexedChars"),
            "total_chars": data.get("totalChars"),
        }

    def download_attachment(self, attachment_key: str) -> dict:
        """Download an attachment's file content, base64-encoded. This
        server runs remotely, so raw bytes can't be handed back as a
        local file path -- the caller must base64-decode
        `content_base64` itself. Read-only."""
        try:
            item = self.zot.item(attachment_key)
            raw = self.zot.file(attachment_key)
        except Exception as exc:
            raise _translate(exc, key=attachment_key) from exc
        summary = _attachment_summary(item)
        summary["content_base64"] = base64.b64encode(raw).decode("ascii")
        return summary

    def list_notes(self, item_key: str) -> list[dict]:
        """List the notes filed under an item -- not its file
        attachments (see list_attachments for those). Read-only. Each
        result includes full note content and `version` to pass to
        update_note."""
        try:
            children = self.zot.children(item_key)
        except Exception as exc:
            raise _translate(exc, key=item_key) from exc
        return [
            _note_summary(c)
            for c in children
            if c.get("data", {}).get("itemType") == "note"
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
        return _item_summary(self._apply_patch(key, version, data))

    def _apply_patch(self, key: str, version: int, data: dict[str, Any]) -> dict:
        """PATCHes `data` onto an existing item and returns the raw,
        post-update item (as pyzotero's zot.item() would). Shared by
        _patch (bibliographic-item callers) and update_note (which needs
        _note_summary instead of _item_summary)."""
        payload = {"key": key, "version": version, **data}
        try:
            self.zot.update_item(payload)
        except Exception as exc:
            raise _translate(exc, key=key) from exc
        try:
            return self.zot.item(key)
        except Exception as exc:
            raise _translate(exc, key=key) from exc

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

    # ---- tags (library-wide) --------------------------------------------

    def list_tags(
        self, query: str | None = None, limit: int = 100, start: int = 0
    ) -> list[str]:
        """List distinct tags used anywhere in the library -- not one
        item's tags (see search_items/get_item for those). Read-only.

        query: substring filter on tag name, or omit to list all tags.
        """
        kwargs: dict[str, Any] = {"limit": limit, "start": start}
        if query:
            kwargs["q"] = query
        try:
            return list(self.zot.tags(**kwargs))
        except Exception as exc:
            raise _translate(exc) from exc

    def rename_tag(self, old_tag: str, new_tag: str) -> dict:
        """Renames a tag across every item in the library that carries it.

        Zotero's API has no dedicated tag-rename endpoint -- this walks
        every item carrying old_tag and, on each, adds new_tag (merging
        with its existing tags) and removes old_tag, one item PATCH per
        item. Not atomic across items: if it fails partway through (e.g.
        a version conflict on one item, from a concurrent edit elsewhere),
        the items already processed will show new_tag while the rest still
        show old_tag. Safe to re-run with the same arguments in that case
        -- already-renamed items no longer carry old_tag, so they're
        simply skipped the second time.
        """
        try:
            items = self.zot.everything(self.zot.items(tag=old_tag))
        except Exception as exc:
            raise _translate(exc) from exc

        updated_keys: list[str] = []
        for item in items:
            data = item.get("data", {})
            key = data.get("key")
            version = data.get("version")
            tags = {t["tag"] for t in data.get("tags", []) if t.get("tag")}
            if old_tag not in tags:
                continue
            tags.discard(old_tag)
            tags.add(new_tag)
            try:
                self._apply_patch(
                    key, version, {"tags": [{"tag": t} for t in sorted(tags)]}
                )
            except Exception as exc:
                raise ZoteroApiError(
                    f"Renamed {old_tag!r} to {new_tag!r} on {len(updated_keys)} "
                    f"item(s) before failing on item {key!r}: {exc}. Re-run "
                    "rename_tag with the same arguments to retry the "
                    "remaining items -- already-renamed ones will be skipped."
                ) from exc
            updated_keys.append(key)

        return {
            "old_tag": old_tag,
            "new_tag": new_tag,
            "items_updated": len(updated_keys),
            "item_keys": updated_keys,
        }

    def delete_tag(self, tag: str) -> dict:
        """DESTRUCTIVE. Permanently removes this tag from every item in
        the library that carries it (not one item -- see remove_tags for
        that). Cannot be undone through this server.

        Uses Zotero's library-wide tag-delete endpoint, which pyzotero
        gates on the library's own current version (fetched internally)
        rather than a version the caller passes in -- unlike item/
        collection deletes, there's no per-tag version to check.
        """
        try:
            self.zot.delete_tags(tag)
        except Exception as exc:
            raise _translate(exc) from exc
        return {"tag": tag, "deleted": True}

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

    def update_collection(
        self,
        key: str,
        version: int,
        name: str | None = None,
        parent_key: str | None = None,
    ) -> dict:
        """Rename and/or move (reparent) a collection, in place. Safe:
        the collection's key is unchanged, so items filed in it and any
        sub-collections stay put.

        name: new name; omit to leave the current name unchanged.
        parent_key: new parent collection's key, to nest this collection
            under it; pass "" (empty string) to move it to the top level
            (out of any parent); omit entirely to leave the parent
            unchanged. At least one of name/parent_key must be given.

        Zotero's collection-update endpoint replaces the whole collection
        record rather than patching individual fields, so this fetches
        the collection's current name/parent first and only overrides
        the field(s) explicitly passed.

        version: the collection's current version (from list_collections)
        -- refused if stale, same as update_item.
        """
        if name is None and parent_key is None:
            raise ValidationError(
                "update_collection requires name and/or parent_key -- "
                "nothing to change"
            )
        try:
            current = self.zot.collection(key)["data"]
        except Exception as exc:
            raise _translate(
                exc, key=key, not_found_cls=CollectionNotFoundError, noun="collection"
            ) from exc
        payload = {
            "key": key,
            "version": version,
            "name": name if name is not None else current.get("name", ""),
            "parentCollection": (
                current.get("parentCollection", False)
                if parent_key is None
                else (parent_key or False)
            ),
        }
        try:
            self.zot.update_collection(payload)
            updated = self.zot.collection(key)
        except Exception as exc:
            raise _translate(
                exc, key=key, not_found_cls=CollectionNotFoundError, noun="collection"
            ) from exc
        return _collection_summary(updated)

    # ---- attachments (create) --------------------------------------------

    def upload_attachment(
        self,
        parent_key: str,
        filename: str,
        content_base64: str,
        title: str | None = None,
    ) -> dict:
        """Upload a new file attachment as a child of an existing item
        (e.g. attach a PDF to a journalArticle item). Safe: creates a
        brand-new attachment item with its own key; never touches the
        parent item's own fields or version.

        filename: name to store the file under, e.g. "paper.pdf" -- also
            used to guess Zotero's contentType from the extension.
        content_base64: the file's bytes, base64-encoded. This server
            runs remotely and has no access to the caller's local
            filesystem, so content must travel as a string rather than a
            local path.
        title: attachment title shown in Zotero; defaults to filename.
        """
        raw = base64.b64decode(content_base64)
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / filename
            path.write_bytes(raw)
            try:
                result = self.zot.attachment_both(
                    [(title or filename, str(path))], parentid=parent_key
                )
            except Exception as exc:
                raise _translate(exc, key=parent_key) from exc

        failures = result.get("failure") or []
        if failures:
            raise ValidationError(f"Zotero rejected the attachment upload: {failures}")
        created = (result.get("success") or result.get("unchanged") or [None])[0]
        if created is None:
            raise ZoteroApiError(f"Upload did not report success: {result}")
        try:
            item = self.zot.item(created["key"])
        except Exception as exc:
            raise _translate(exc, key=created.get("key")) from exc
        return _attachment_summary(item)

    # ---- notes (create) ---------------------------------------------------

    def create_note(
        self, parent_key: str, content: str, tags: list[str] | None = None
    ) -> dict:
        """Add a new note as a child of an existing item (e.g. a research
        note attached to a journalArticle). Safe: creates a brand-new
        note item with its own key; never touches the parent item's own
        fields or version.

        content: the note's body, as Zotero-flavored HTML (e.g.
            "<p>Some observation.</p>") -- Zotero derives the note's
            display title from the first line of this content.
        """
        try:
            template = self.zot.item_template("note")
        except Exception as exc:
            raise _translate(exc) from exc
        template["note"] = content
        if tags is not None:
            template["tags"] = [{"tag": t} for t in tags]
        try:
            result = self.zot.create_items([template], parentid=parent_key)
        except Exception as exc:
            raise _translate(exc, key=parent_key) from exc

        failed = result.get("failed") or {}
        if failed:
            raise ValidationError(f"Zotero rejected the new note: {failed}")
        success = result.get("successful") or result.get("success") or {}
        created = next(iter(success.values()))
        if isinstance(created, dict):
            return _note_summary(created)
        try:
            return _note_summary(self.zot.item(created))
        except Exception as exc:
            raise _translate(exc, key=created) from exc

    def update_note(self, key: str, version: int, content: str) -> dict:
        """Edit a note's content, in place. Safe, key-preserving.
        version: the note's current version (from list_notes)."""
        return _note_summary(self._apply_patch(key, version, {"note": content}))

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

    def delete_collection(self, key: str, version: int) -> dict:
        """DESTRUCTIVE. Permanently deletes the collection. Matches
        Zotero's own "Delete Collection" behavior: any sub-collections
        nested under it are deleted too, cascading -- but items filed in
        it (or in a deleted sub-collection) are NOT deleted from the
        library, only unfiled from that collection.

        There is no confirmation step at this layer; the caller is
        responsible for making sure this is really what's wanted,
        including checking list_collections for sub-collections first if
        that matters. `version` must match the collection's current
        version or the call is refused (VersionConflictError).
        """
        try:
            self.zot.delete_collection({"key": key, "version": version})
        except Exception as exc:
            raise _translate(
                exc, key=key, not_found_cls=CollectionNotFoundError, noun="collection"
            ) from exc
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
