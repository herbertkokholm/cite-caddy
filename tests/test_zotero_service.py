import pytest

from app.zotero_service import (
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
    assert created["creators"] == "Ada Lovelace"
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

    assert updated["creators"] == "B Two"


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
