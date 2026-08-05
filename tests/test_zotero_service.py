import base64

import pytest

from app.zotero_service import (
    CollectionNotFoundError,
    ItemNotFoundError,
    ValidationError,
    VersionConflictError,
)

# ---- read ----------------------------------------------------------------


def test_search_items_by_query(service, fake_zot):
    fake_zot.seed_item("journalArticle", {"title": "Deep Learning for X"})
    fake_zot.seed_item("journalArticle", {"title": "Unrelated Paper"})

    results = service.search_items(query="deep learning")

    assert len(results) == 1
    assert results[0]["title"] == "Deep Learning for X"
    assert "version" in results[0]


def test_search_items_full_text_matches_attachment_content(service, fake_zot):
    hit = fake_zot.seed_item("journalArticle", {"title": "Unrelated Title"})
    fake_zot.seed_item("journalArticle", {"title": "Also Unrelated"})
    fake_zot.seed_fulltext(hit["key"], "contains the phrase quantum entanglement")

    results = service.search_items(query="quantum entanglement", full_text=True)

    assert [r["key"] for r in results] == [hit["key"]]


def test_search_items_full_text_still_matches_title(service, fake_zot):
    hit = fake_zot.seed_item("journalArticle", {"title": "Deep Learning for X"})
    fake_zot.seed_item("journalArticle", {"title": "Unrelated Paper"})

    results = service.search_items(query="deep learning", full_text=True)

    assert [r["key"] for r in results] == [hit["key"]]


def test_search_items_full_text_without_query_is_rejected(service):
    with pytest.raises(ValidationError):
        service.search_items(full_text=True)


def test_search_items_by_collection(service, fake_zot):
    coll = fake_zot.seed_collection("My Collection")
    in_coll = fake_zot.seed_item("book", {"title": "In", "collections": [coll["key"]]})
    fake_zot.seed_item("book", {"title": "Out"})

    results = service.search_items(collection_key=coll["key"])

    assert [r["key"] for r in results] == [in_coll["key"]]


def test_get_item_not_found(service):
    with pytest.raises(ItemNotFoundError):
        service.get_item("MISSING")


def test_list_collections(service, fake_zot):
    fake_zot.seed_collection("Papers")
    result = service.list_collections()
    assert result[0]["name"] == "Papers"


# ---- creators ---------------------------------------------------------------


def test_get_item_creators_distinguishes_authors_and_editors(service, fake_zot):
    item = fake_zot.seed_item(
        "conferencePaper",
        {
            "creators": [
                {"creatorType": "author", "firstName": "Debra", "lastName": "Nestel"},
                {
                    "creatorType": "editor",
                    "firstName": "Margaret",
                    "lastName": "Bearman",
                },
            ]
        },
    )

    result = service.get_item(item["key"])

    assert result["creators"] == [
        {"creatorType": "author", "firstName": "Debra", "lastName": "Nestel"},
        {"creatorType": "editor", "firstName": "Margaret", "lastName": "Bearman"},
    ]


def test_get_item_creators_institutional_single_field(service, fake_zot):
    item = fake_zot.seed_item(
        "report",
        {"creators": [{"creatorType": "author", "name": "United Nations"}]},
    )

    result = service.get_item(item["key"])

    assert result["creators"] == [{"creatorType": "author", "name": "United Nations"}]


def test_get_item_creators_empty(service, fake_zot):
    item = fake_zot.seed_item("journalArticle")

    result = service.get_item(item["key"])

    assert result["creators"] == []


def test_search_items_creators_matches_get_item_shape(service, fake_zot):
    fake_zot.seed_item(
        "book",
        {
            "title": "Many-Author Volume",
            "creators": [
                {
                    "creatorType": "author",
                    "firstName": f"First{i}",
                    "lastName": f"Last{i}",
                }
                for i in range(15)
            ],
        },
    )

    [result] = service.search_items(query="Many-Author Volume")

    assert len(result["creators"]) == 15
    assert result["creators"][0] == {
        "creatorType": "author",
        "firstName": "First0",
        "lastName": "Last0",
    }


def test_create_item_creators_round_trip_into_update_item(service, fake_zot):
    created = service.create_item(
        "book",
        fields={"title": "Round Trip"},
        creators=[
            {"creatorType": "editor", "firstName": "Ada", "lastName": "Lovelace"}
        ],
    )

    updated = service.update_item(
        created["key"],
        version=created["version"],
        fields={"creators": created["creators"]},
    )

    assert updated["creators"] == created["creators"]


# ---- saved searches --------------------------------------------------------


def test_create_and_list_saved_search(service):
    conditions = [{"condition": "itemType", "operator": "is", "value": "book"}]
    created = service.create_saved_search("Books", conditions)

    assert created["name"] == "Books"
    assert created["conditions"] == conditions

    listed = service.list_saved_searches()
    assert [s["key"] for s in listed] == [created["key"]]


def test_delete_saved_search(service):
    created = service.create_saved_search(
        "Doomed", [{"condition": "itemType", "operator": "is", "value": "book"}]
    )

    result = service.delete_saved_search(created["key"])

    assert result == {"key": created["key"], "deleted": True}
    assert service.list_saved_searches() == []


# ---- groups ----------------------------------------------------------------


def test_list_groups(service, fake_zot):
    fake_zot.seed_group("Lab Group", group_id=555, type="Private")

    result = service.list_groups()

    assert result == [
        {
            "id": 555,
            "name": "Lab Group",
            "type": "Private",
            "owner": 1,
            "library_editing": "members",
            "library_reading": "members",
        }
    ]


# ---- schema introspection --------------------------------------------------


def test_list_item_types(service):
    result = service.list_item_types()
    assert {"item_type": "book", "localized": "Book"} in result


def test_list_item_fields(service):
    result = service.list_item_fields()
    assert {"field": "title", "localized": "Title"} in result


def test_list_item_type_fields(service):
    result = service.list_item_type_fields("book")
    assert {"field": "date", "localized": "Date"} in result


def test_list_item_creator_types(service):
    result = service.list_item_creator_types("book")
    assert {"creator_type": "author", "localized": "Author"} in result


# ---- trash ---------------------------------------------------------------


def test_list_trash_excludes_active_items(service, fake_zot):
    active = fake_zot.seed_item("book", {"title": "Active"})
    trashed = fake_zot.seed_item("book", {"title": "Trashed", "deleted": 1})

    result = service.list_trash()

    assert [r["key"] for r in result] == [trashed["key"]]
    assert result[0]["in_trash"] is True
    assert active["key"] not in [r["key"] for r in result]


def test_trash_item_removes_from_normal_listing(service, fake_zot):
    item = fake_zot.seed_item("book", {"title": "Doomed"})

    service.trash_item(item["key"], version=1)

    assert service.search_items() == []
    assert [r["key"] for r in service.list_trash()] == [item["key"]]


def test_restore_from_trash(service, fake_zot):
    item = fake_zot.seed_item("book", {"title": "Recoverable", "deleted": 1})

    restored = service.restore_from_trash(item["key"], version=1)

    assert restored["in_trash"] is False
    assert [r["key"] for r in service.search_items()] == [item["key"]]
    assert service.list_trash() == []


def test_trash_item_then_restore_round_trip(service, fake_zot):
    item = fake_zot.seed_item("book", {"title": "Round trip"})

    trashed = service.trash_item(item["key"], version=1)
    restored = service.restore_from_trash(item["key"], version=trashed["version"])

    assert restored["key"] == item["key"]
    assert restored["in_trash"] is False


def test_update_item_tool_rejects_deleted_field(service, fake_zot):
    item = fake_zot.seed_item("book")
    with pytest.raises(ValidationError):
        service.update_item(item["key"], version=1, fields={"deleted": 1})


# ---- attachments & fulltext (read) -----------------------------------------


def test_list_attachments_excludes_notes(service, fake_zot):
    item = fake_zot.seed_item("journalArticle")
    attachment = fake_zot.seed_attachment(item["key"], title="paper.pdf")
    # A note child, if seeded directly into fake_zot, must be filtered out.
    fake_zot._items["NOTE1"] = {
        "key": "NOTE1",
        "version": 1,
        "data": {
            "key": "NOTE1",
            "version": 1,
            "itemType": "note",
            "parentItem": item["key"],
            "tags": [],
        },
    }

    results = service.list_attachments(item["key"])

    assert [r["key"] for r in results] == [attachment["key"]]
    assert results[0]["filename"] == "paper.pdf"
    assert results[0]["content_type"] == "application/pdf"


def test_list_attachments_item_not_found(service):
    with pytest.raises(ItemNotFoundError):
        service.list_attachments("MISSING")


def test_get_fulltext(service, fake_zot):
    attachment = fake_zot.seed_attachment("PARENT1")
    fake_zot.seed_fulltext(
        attachment["key"], "extracted text", indexedPages=3, totalPages=3
    )

    result = service.get_fulltext(attachment["key"])

    assert result["content"] == "extracted text"
    assert result["indexed_pages"] == 3
    assert result["total_pages"] == 3


def test_get_fulltext_not_found(service):
    with pytest.raises(ItemNotFoundError):
        service.get_fulltext("MISSING")


def test_download_attachment(service, fake_zot):
    attachment = fake_zot.seed_attachment(
        "PARENT1", filename="paper.pdf", content=b"%PDF-1.4 fake bytes"
    )

    result = service.download_attachment(attachment["key"])

    assert result["filename"] == "paper.pdf"
    assert result["content_type"] == "application/pdf"
    assert base64.b64decode(result["content_base64"]) == b"%PDF-1.4 fake bytes"


def test_download_attachment_not_found(service):
    with pytest.raises(ItemNotFoundError):
        service.download_attachment("MISSING")


# ---- notes -------------------------------------------------------------


def test_create_note(service, fake_zot):
    parent = fake_zot.seed_item("journalArticle", {"title": "Host Item"})

    created = service.create_note(
        parent["key"], "<p>Interesting finding.</p>", tags=["important"]
    )

    assert created["content"] == "<p>Interesting finding.</p>"
    assert created["parent_item"] == parent["key"]
    assert created["tags"] == ["important"]
    assert created["key"]


def test_create_note_parent_not_found(service):
    with pytest.raises(ItemNotFoundError):
        service.create_note("MISSING", "<p>Orphaned.</p>")


def test_list_notes_excludes_attachments(service, fake_zot):
    item = fake_zot.seed_item("journalArticle")
    note = fake_zot.seed_note(item["key"], "<p>Note content.</p>")
    fake_zot.seed_attachment(item["key"], title="paper.pdf")

    results = service.list_notes(item["key"])

    assert [r["key"] for r in results] == [note["key"]]
    assert results[0]["content"] == "<p>Note content.</p>"


def test_list_notes_item_not_found(service):
    with pytest.raises(ItemNotFoundError):
        service.list_notes("MISSING")


def test_update_note(service, fake_zot):
    item = fake_zot.seed_item("journalArticle")
    note = fake_zot.seed_note(item["key"], "<p>Old content.</p>")

    updated = service.update_note(note["key"], version=1, content="<p>New content.</p>")

    assert updated["content"] == "<p>New content.</p>"
    assert updated["key"] == note["key"]  # key preserved


def test_update_note_version_conflict(service, fake_zot):
    item = fake_zot.seed_item("journalArticle")
    note = fake_zot.seed_note(item["key"], "<p>Old.</p>")
    service.update_note(note["key"], version=1, content="<p>Changed elsewhere.</p>")

    with pytest.raises(VersionConflictError):
        service.update_note(note["key"], version=1, content="<p>Clobber attempt.</p>")


# ---- attachments (create) ---------------------------------------------------


def test_upload_attachment(service, fake_zot):
    parent = fake_zot.seed_item("journalArticle", {"title": "Host Item"})
    content = base64.b64encode(b"hello pdf bytes").decode("ascii")

    result = service.upload_attachment(
        parent["key"], "notes.pdf", content, title="My Notes"
    )

    assert result["title"] == "My Notes"
    assert result["filename"] == "notes.pdf"
    assert result["parent_item"] == parent["key"]
    assert result["content_type"] == "application/pdf"

    # Round-trips through download_attachment against the same fake store.
    downloaded = service.download_attachment(result["key"])
    assert base64.b64decode(downloaded["content_base64"]) == b"hello pdf bytes"


def test_upload_attachment_defaults_title_to_filename(service, fake_zot):
    parent = fake_zot.seed_item("journalArticle")
    content = base64.b64encode(b"data").decode("ascii")

    result = service.upload_attachment(parent["key"], "untitled.txt", content)

    assert result["title"] == "untitled.txt"


def test_upload_attachment_parent_not_found(service):
    content = base64.b64encode(b"data").decode("ascii")
    with pytest.raises(ItemNotFoundError):
        service.upload_attachment("MISSING", "f.pdf", content)


# ---- create ----------------------------------------------------------------


def test_create_item(service):
    created = service.create_item(
        "journalArticle",
        fields={"title": "New Paper", "DOI": "10.1/x"},
        creators=[
            {"creatorType": "author", "firstName": "Ada", "lastName": "Lovelace"}
        ],
        tags=["ml"],
    )
    assert created["title"] == "New Paper"
    assert created["doi"] == "10.1/x"
    assert created["creators"] == [
        {"creatorType": "author", "firstName": "Ada", "lastName": "Lovelace"}
    ]
    assert created["tags"] == ["ml"]
    assert created["key"]


# ---- update (key-preserving) ------------------------------------------------


def test_update_item_patches_fields(service, fake_zot):
    item = fake_zot.seed_item("journalArticle", {"title": "Old Title"})

    updated = service.update_item(item["key"], version=1, fields={"title": "New Title"})

    assert updated["title"] == "New Title"
    assert updated["key"] == item["key"]  # key preserved


def test_update_item_allows_creators_replace(service, fake_zot):
    item = fake_zot.seed_item(
        "book",
        {"creators": [{"creatorType": "author", "firstName": "A", "lastName": "One"}]},
    )

    updated = service.update_item(
        item["key"],
        version=1,
        fields={
            "creators": [{"creatorType": "author", "firstName": "B", "lastName": "Two"}]
        },
    )

    assert updated["creators"] == [
        {"creatorType": "author", "firstName": "B", "lastName": "Two"}
    ]


def test_update_item_rejects_reserved_fields(service, fake_zot):
    item = fake_zot.seed_item("journalArticle")
    with pytest.raises(ValidationError):
        service.update_item(item["key"], version=1, fields={"tags": [{"tag": "x"}]})


def test_update_item_version_conflict(service, fake_zot):
    item = fake_zot.seed_item("journalArticle", {"title": "Old"})
    service.update_item(item["key"], version=1, fields={"title": "Changed elsewhere"})

    # Caller still has the stale version=1 and tries to write on top of it.
    with pytest.raises(VersionConflictError):
        service.update_item(item["key"], version=1, fields={"title": "Clobber attempt"})


# ---- tags --------------------------------------------------------------


def test_add_tags_merges_with_existing(service, fake_zot):
    item = fake_zot.seed_item("book", {"tags": [{"tag": "existing"}]})

    updated = service.add_tags(item["key"], version=1, tags=["new"])

    assert sorted(updated["tags"]) == ["existing", "new"]


def test_remove_tags(service, fake_zot):
    item = fake_zot.seed_item("book", {"tags": [{"tag": "a"}, {"tag": "b"}]})

    updated = service.remove_tags(item["key"], version=1, tags=["a"])

    assert updated["tags"] == ["b"]


def test_set_tags_replaces(service, fake_zot):
    item = fake_zot.seed_item("book", {"tags": [{"tag": "a"}]})

    updated = service.set_tags(item["key"], version=1, tags=["only-this"])

    assert updated["tags"] == ["only-this"]


# ---- tags (library-wide) ------------------------------------------------


def test_list_tags(service, fake_zot):
    fake_zot.seed_item("book", {"tags": [{"tag": "alpha"}]})
    fake_zot.seed_item("book", {"tags": [{"tag": "beta"}]})

    assert service.list_tags() == ["alpha", "beta"]


def test_list_tags_filters_by_query(service, fake_zot):
    fake_zot.seed_item("book", {"tags": [{"tag": "alpha"}]})
    fake_zot.seed_item("book", {"tags": [{"tag": "beta"}]})

    assert service.list_tags(query="al") == ["alpha"]


def test_rename_tag_across_items(service, fake_zot):
    a = fake_zot.seed_item("book", {"tags": [{"tag": "old"}, {"tag": "keep"}]})
    b = fake_zot.seed_item("book", {"tags": [{"tag": "old"}]})
    untouched = fake_zot.seed_item("book", {"tags": [{"tag": "other"}]})

    result = service.rename_tag("old", "new")

    assert result["items_updated"] == 2
    assert sorted(result["item_keys"]) == sorted([a["key"], b["key"]])
    assert sorted(service.get_item(a["key"])["tags"]) == ["keep", "new"]
    assert service.get_item(b["key"])["tags"] == ["new"]
    assert service.get_item(untouched["key"])["tags"] == ["other"]


def test_rename_tag_merges_if_target_already_present(service, fake_zot):
    item = fake_zot.seed_item("book", {"tags": [{"tag": "old"}, {"tag": "new"}]})

    result = service.rename_tag("old", "new")

    assert result["items_updated"] == 1
    assert service.get_item(item["key"])["tags"] == ["new"]


def test_rename_tag_no_matching_items(service, fake_zot):
    fake_zot.seed_item("book", {"tags": [{"tag": "unrelated"}]})

    result = service.rename_tag("old", "new")

    assert result["items_updated"] == 0
    assert result["item_keys"] == []


def test_delete_tag(service, fake_zot):
    a = fake_zot.seed_item("book", {"tags": [{"tag": "doomed"}, {"tag": "keep"}]})
    b = fake_zot.seed_item("book", {"tags": [{"tag": "unrelated"}]})

    result = service.delete_tag("doomed")

    assert result == {"tag": "doomed", "deleted": True}
    assert service.get_item(a["key"])["tags"] == ["keep"]
    assert service.get_item(b["key"])["tags"] == ["unrelated"]


# ---- collection membership (safe "move") --------------------------------


def test_add_to_collection(service, fake_zot):
    coll = fake_zot.seed_collection("Target")
    item = fake_zot.seed_item("book")

    updated = service.add_to_collection(
        item["key"], version=1, collection_key=coll["key"]
    )

    assert updated["collections"] == [coll["key"]]
    assert (
        updated["key"] == item["key"]
    )  # key preserved -- this is not move_item_to_library


def test_remove_from_collection(service, fake_zot):
    coll = fake_zot.seed_collection("Target")
    item = fake_zot.seed_item("book", {"collections": [coll["key"]]})

    updated = service.remove_from_collection(
        item["key"], version=1, collection_key=coll["key"]
    )

    assert updated["collections"] == []


def test_create_collection(service):
    created = service.create_collection("New Collection")
    assert created["name"] == "New Collection"
    assert created["key"]


# ---- collection management -------------------------------------------------


def test_update_collection_renames(service, fake_zot):
    coll = fake_zot.seed_collection("Old Name")

    updated = service.update_collection(coll["key"], version=1, name="New Name")

    assert updated["name"] == "New Name"
    assert updated["key"] == coll["key"]  # key preserved


def test_update_collection_reparents_without_touching_name(service, fake_zot):
    parent = fake_zot.seed_collection("Parent")
    coll = fake_zot.seed_collection("Child", parent_key=None)

    updated = service.update_collection(
        coll["key"], version=1, parent_key=parent["key"]
    )

    assert updated["name"] == "Child"  # unchanged
    assert updated["parent_collection"] == parent["key"]


def test_update_collection_moves_to_top_level(service, fake_zot):
    parent = fake_zot.seed_collection("Parent")
    coll = fake_zot.seed_collection("Child", parent_key=parent["key"])

    updated = service.update_collection(coll["key"], version=1, parent_key="")

    assert updated["parent_collection"] is None


def test_update_collection_requires_a_field(service, fake_zot):
    coll = fake_zot.seed_collection("Name")
    with pytest.raises(ValidationError):
        service.update_collection(coll["key"], version=1)


def test_update_collection_version_conflict(service, fake_zot):
    coll = fake_zot.seed_collection("Old")
    service.update_collection(coll["key"], version=1, name="Changed elsewhere")

    with pytest.raises(VersionConflictError):
        service.update_collection(coll["key"], version=1, name="Clobber attempt")


def test_update_collection_not_found(service):
    with pytest.raises(CollectionNotFoundError):
        service.update_collection("MISSING", version=1, name="X")


def test_delete_collection_not_found(service):
    with pytest.raises(CollectionNotFoundError):
        service.delete_collection("MISSING", version=1)


def test_delete_collection(service, fake_zot):
    coll = fake_zot.seed_collection("Doomed")

    result = service.delete_collection(coll["key"], version=1)

    assert result["deleted"] is True
    assert service.list_collections() == []


def test_delete_collection_version_conflict_does_not_delete(service, fake_zot):
    coll = fake_zot.seed_collection("Old")
    service.update_collection(coll["key"], version=1, name="Changed elsewhere")

    with pytest.raises(VersionConflictError):
        service.delete_collection(coll["key"], version=1)

    assert len(service.list_collections()) == 1


# ---- destructive: delete ------------------------------------------------


def test_delete_item(service, fake_zot):
    item = fake_zot.seed_item("book")

    result = service.delete_item(item["key"], version=1)

    assert result["deleted"] is True
    with pytest.raises(ItemNotFoundError):
        service.get_item(item["key"])


def test_delete_item_version_conflict_does_not_delete(service, fake_zot):
    item = fake_zot.seed_item("book", {"title": "Old"})
    service.update_item(item["key"], version=1, fields={"title": "Changed elsewhere"})

    with pytest.raises(VersionConflictError):
        service.delete_item(item["key"], version=1)

    # Item must still exist -- the stale-version delete must not have gone through.
    assert service.get_item(item["key"])["title"] == "Changed elsewhere"


# ---- destructive: cross-library move -------------------------------------


def test_move_item_to_library_refuses_stale_version_before_creating_in_target(
    service, fake_zot
):
    item = fake_zot.seed_item("book", {"title": "Old"})
    service.update_item(item["key"], version=1, fields={"title": "Changed elsewhere"})

    import app.zotero_service as mod

    original_zotero_cls = mod.zotero.Zotero
    mod.zotero.Zotero = lambda *a, **kw: fake_zot
    try:
        with pytest.raises(VersionConflictError):
            service.move_item_to_library(
                item["key"],
                version=1,
                target_library_id="456",
                target_library_type="group",
            )
    finally:
        mod.zotero.Zotero = original_zotero_cls

    # No orphaned copy should have been created in the "target" library
    # (the same fake store here) -- only the one original item exists.
    assert len(fake_zot.items()) == 1


def test_move_item_to_library(service, fake_zot):
    item = fake_zot.seed_item(
        "journalArticle",
        {"title": "Portable Paper", "collections": ["SOME_COLLECTION"]},
    )

    # move_item_to_library creates a second Zotero client for the target
    # library; patch the client class the service module imports so that
    # second client is the same fake instance instead of a real pyzotero one.
    import app.zotero_service as mod

    original_zotero_cls = mod.zotero.Zotero
    mod.zotero.Zotero = lambda *a, **kw: fake_zot
    try:
        result = service.move_item_to_library(
            item["key"],
            version=1,
            target_library_id="456",
            target_library_type="group",
        )
    finally:
        mod.zotero.Zotero = original_zotero_cls

    assert result["old_key"] == item["key"]
    assert result["new_key"] != item["key"]
    assert result["deleted_from_source"] is True
    assert item["key"] in result["warning"]

    # Original item is gone from the source; new item exists (in the same
    # fake store here, since target and source share fake_zot in this test).
    with pytest.raises(ItemNotFoundError):
        service.get_item(item["key"])
    assert service.get_item(result["new_key"])["title"] == "Portable Paper"
    # Collections are not carried over to the new library -- they're
    # library-specific keys that wouldn't exist (or mean the same thing) there.
    assert service.get_item(result["new_key"])["collections"] == []
