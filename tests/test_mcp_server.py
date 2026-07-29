"""Tests the MCP tool functions in app/mcp_server.py directly (FastMCP's
@mcp.tool() decorator returns the plain function, so these are callable
without going through the MCP protocol/transport at all) against a
ZoteroService backed by the in-memory FakeZotero -- never a real Zotero
client, per README's testing note.
"""

import base64

import pytest

from app import mcp_server
from app.zotero_service import (
    ItemNotFoundError,
    ValidationError,
    VersionConflictError,
    ZoteroService,
)
from tests.fakes import FakeZotero


@pytest.fixture
def fake_zot() -> FakeZotero:
    return FakeZotero()


@pytest.fixture(autouse=True)
def fake_service(fake_zot: FakeZotero):
    mcp_server.configure_service(ZoteroService(fake_zot, api_key="fake-key"))
    return fake_zot


def test_search_items_tool(fake_service):
    fake_service.seed_item("book", {"title": "Findable"})
    results = mcp_server.search_items(query="findable")
    assert results[0]["title"] == "Findable"


def test_search_items_tool_full_text(fake_service):
    hit = fake_service.seed_item("book", {"title": "Unrelated Title"})
    fake_service.seed_fulltext(hit["key"], "mentions cryptographic protocols")

    results = mcp_server.search_items(query="cryptographic protocols", full_text=True)

    assert [r["key"] for r in results] == [hit["key"]]


def test_search_items_tool_full_text_without_query_rejected(fake_service):
    with pytest.raises(ValidationError):
        mcp_server.search_items(full_text=True)


def test_get_item_tool_not_found(fake_service):
    with pytest.raises(ItemNotFoundError):
        mcp_server.get_item("NOPE")


def test_create_item_tool(fake_service):
    created = mcp_server.create_item("journalArticle", fields={"title": "New"})
    assert created["title"] == "New"


def test_update_item_tool_round_trip(fake_service):
    item = fake_service.seed_item("book", {"title": "Old"})
    updated = mcp_server.update_item(item["key"], version=1, fields={"title": "New"})
    assert updated["title"] == "New"


def test_update_item_tool_rejects_tags_field(fake_service):
    item = fake_service.seed_item("book")
    with pytest.raises(ValidationError):
        mcp_server.update_item(item["key"], version=1, fields={"tags": []})


def test_tag_tools(fake_service):
    item = fake_service.seed_item("book", {"tags": [{"tag": "a"}]})
    updated = mcp_server.add_tags(item["key"], version=1, tags=["b"])
    assert sorted(updated["tags"]) == ["a", "b"]

    updated = mcp_server.remove_tags(
        item["key"], version=updated["version"], tags=["a"]
    )
    assert updated["tags"] == ["b"]

    updated = mcp_server.set_tags(
        item["key"], version=updated["version"], tags=["only"]
    )
    assert updated["tags"] == ["only"]


def test_saved_search_tools(fake_service):
    conditions = [{"condition": "itemType", "operator": "is", "value": "book"}]
    created = mcp_server.create_saved_search("Books", conditions)
    assert created["name"] == "Books"

    listed = mcp_server.list_saved_searches()
    assert [s["key"] for s in listed] == [created["key"]]

    result = mcp_server.delete_saved_search(created["key"])
    assert result["deleted"] is True
    assert mcp_server.list_saved_searches() == []


def test_list_groups_tool(fake_service):
    fake_service.seed_group("Lab Group", group_id=42)
    result = mcp_server.list_groups()
    assert result[0]["id"] == 42
    assert result[0]["name"] == "Lab Group"


def test_schema_introspection_tools(fake_service):
    assert {"item_type": "book", "localized": "Book"} in mcp_server.list_item_types()
    assert {"field": "title", "localized": "Title"} in mcp_server.list_item_fields()
    assert {"field": "date", "localized": "Date"} in mcp_server.list_item_type_fields(
        "book"
    )
    assert {
        "creator_type": "author",
        "localized": "Author",
    } in mcp_server.list_item_creator_types("book")


def test_trash_tools(fake_service):
    item = fake_service.seed_item("book", {"title": "Doomed"})

    trashed = mcp_server.trash_item(item["key"], version=1)
    assert trashed["in_trash"] is True
    assert mcp_server.search_items() == []

    listed = mcp_server.list_trash()
    assert [i["key"] for i in listed] == [item["key"]]

    restored = mcp_server.restore_from_trash(item["key"], version=trashed["version"])
    assert restored["in_trash"] is False
    assert mcp_server.list_trash() == []


def test_library_tag_tools(fake_service):
    a = fake_service.seed_item("book", {"tags": [{"tag": "old"}]})
    b = fake_service.seed_item("book", {"tags": [{"tag": "other"}]})

    assert sorted(mcp_server.list_tags()) == ["old", "other"]

    renamed = mcp_server.rename_tag("old", "new")
    assert renamed["items_updated"] == 1
    assert mcp_server.get_item(a["key"])["tags"] == ["new"]

    result = mcp_server.delete_tag("other")
    assert result["deleted"] is True
    assert mcp_server.get_item(b["key"])["tags"] == []


def test_collection_tools(fake_service):
    coll = mcp_server.create_collection("Papers")
    assert coll["name"] == "Papers"

    item = fake_service.seed_item("book")
    updated = mcp_server.add_to_collection(
        item["key"], version=1, collection_key=coll["key"]
    )
    assert updated["collections"] == [coll["key"]]

    updated = mcp_server.remove_from_collection(
        item["key"], version=updated["version"], collection_key=coll["key"]
    )
    assert updated["collections"] == []


def test_update_and_delete_collection_tools(fake_service):
    coll = mcp_server.create_collection("Old Name")

    renamed = mcp_server.update_collection(coll["key"], version=1, name="New Name")
    assert renamed["name"] == "New Name"

    result = mcp_server.delete_collection(renamed["key"], version=renamed["version"])
    assert result["deleted"] is True
    assert mcp_server.list_collections() == []


def test_attachment_and_fulltext_tools(fake_service):
    parent = fake_service.seed_item("journalArticle", {"title": "Host"})
    content = base64.b64encode(b"pdf bytes").decode("ascii")

    uploaded = mcp_server.upload_attachment(
        parent["key"], "paper.pdf", content, title="Paper"
    )
    assert uploaded["title"] == "Paper"
    assert uploaded["parent_item"] == parent["key"]

    listed = mcp_server.list_attachments(parent["key"])
    assert [a["key"] for a in listed] == [uploaded["key"]]

    downloaded = mcp_server.download_attachment(uploaded["key"])
    assert base64.b64decode(downloaded["content_base64"]) == b"pdf bytes"

    fake_service.seed_fulltext(uploaded["key"], "extracted text")
    fulltext = mcp_server.get_fulltext(uploaded["key"])
    assert fulltext["content"] == "extracted text"


def test_note_tools(fake_service):
    parent = fake_service.seed_item("journalArticle", {"title": "Host"})

    created = mcp_server.create_note(
        parent["key"], "<p>First thought.</p>", tags=["todo"]
    )
    assert created["content"] == "<p>First thought.</p>"
    assert created["parent_item"] == parent["key"]
    assert created["tags"] == ["todo"]

    listed = mcp_server.list_notes(parent["key"])
    assert [n["key"] for n in listed] == [created["key"]]

    updated = mcp_server.update_note(
        created["key"], version=created["version"], content="<p>Revised.</p>"
    )
    assert updated["content"] == "<p>Revised.</p>"


def test_list_collections_tool(fake_service):
    fake_service.seed_collection("Papers")
    result = mcp_server.list_collections()
    assert result[0]["name"] == "Papers"


def test_delete_item_permanently_tool(fake_service):
    item = fake_service.seed_item("book")
    result = mcp_server.delete_item_permanently(item["key"], version=1)
    assert result["deleted"] is True
    with pytest.raises(ItemNotFoundError):
        mcp_server.get_item(item["key"])


def test_delete_item_permanently_tool_refuses_stale_version(fake_service):
    item = fake_service.seed_item("book", {"title": "Old"})
    mcp_server.update_item(item["key"], version=1, fields={"title": "Changed"})
    with pytest.raises(VersionConflictError):
        mcp_server.delete_item_permanently(item["key"], version=1)


def test_move_item_to_different_library_tool(fake_service, monkeypatch):
    item = fake_service.seed_item("journalArticle", {"title": "Portable"})

    import app.zotero_service as svc_mod

    monkeypatch.setattr(svc_mod.zotero, "Zotero", lambda *a, **kw: fake_service)

    result = mcp_server.move_item_to_different_library(
        item["key"], version=1, target_library_id="456", target_library_type="group"
    )

    assert result["old_key"] == item["key"]
    assert result["new_key"] != item["key"]
    with pytest.raises(ItemNotFoundError):
        mcp_server.get_item(item["key"])
