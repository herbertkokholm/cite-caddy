import os

from app.oauth_store import TokenStore


def test_put_get_delete_roundtrip(tmp_path):
    store = TokenStore(str(tmp_path / "store.json"))

    store.put("clients", "abc", {"client_id": "abc", "name": "test"})
    assert store.get("clients", "abc") == {"client_id": "abc", "name": "test"}

    store.delete("clients", "abc")
    assert store.get("clients", "abc") is None


def test_get_missing_returns_none(tmp_path):
    store = TokenStore(str(tmp_path / "store.json"))
    assert store.get("access_tokens", "nope") is None


def test_persists_across_instances(tmp_path):
    path = str(tmp_path / "store.json")
    TokenStore(path).put("refresh_tokens", "r1", {"token": "r1"})

    reloaded = TokenStore(path)
    assert reloaded.get("refresh_tokens", "r1") == {"token": "r1"}


def test_creates_parent_directory(tmp_path):
    path = str(tmp_path / "nested" / "dir" / "store.json")
    store = TokenStore(path)
    store.put("clients", "x", {"a": 1})
    assert os.path.exists(path)
