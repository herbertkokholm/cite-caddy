import json
import os

import pytest
from cryptography.fernet import Fernet, InvalidToken

from app.oauth_store import TokenStore

FERNET_KEY = Fernet.generate_key().decode()


def _store(path: str, fernet_key: str = FERNET_KEY) -> TokenStore:
    return TokenStore(path, fernet_key=fernet_key)


def test_put_get_delete_roundtrip(tmp_path):
    store = _store(str(tmp_path / "store.json"))

    store.put("clients", "abc", {"client_id": "abc", "name": "test"})
    assert store.get("clients", "abc") == {"client_id": "abc", "name": "test"}

    store.delete("clients", "abc")
    assert store.get("clients", "abc") is None


def test_get_missing_returns_none(tmp_path):
    store = _store(str(tmp_path / "store.json"))
    assert store.get("access_tokens", "nope") is None


def test_persists_across_instances(tmp_path):
    path = str(tmp_path / "store.json")
    _store(path).put("refresh_tokens", "r1", {"token": "r1"})

    reloaded = _store(path)
    assert reloaded.get("refresh_tokens", "r1") == {"token": "r1"}


def test_creates_parent_directory(tmp_path):
    path = str(tmp_path / "nested" / "dir" / "store.json")
    store = _store(path)
    store.put("clients", "x", {"a": 1})
    assert os.path.exists(path)


def test_backfills_missing_collections_from_older_store_file(tmp_path):
    path = str(tmp_path / "store.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"clients": {"c1": {"a": 1}}}, f)

    store = _store(path)
    assert store.get("clients", "c1") == {"a": 1}
    assert store.get("tenants", "123") is None


# ---- tenants (encrypted at rest) -------------------------------------------


def test_tenant_roundtrip(tmp_path):
    store = _store(str(tmp_path / "store.json"))
    store.put_tenant(
        "123", {"library_id": "123", "library_type": "user", "api_key": "s3cr3t"}
    )

    tenant = store.get_tenant("123")
    assert tenant == {"library_id": "123", "library_type": "user", "api_key": "s3cr3t"}


def test_tenant_missing_returns_none(tmp_path):
    store = _store(str(tmp_path / "store.json"))
    assert store.get_tenant("nope") is None


def test_tenant_upsert_overwrites(tmp_path):
    store = _store(str(tmp_path / "store.json"))
    store.put_tenant(
        "123", {"library_id": "123", "library_type": "user", "api_key": "old-key"}
    )
    store.put_tenant(
        "123", {"library_id": "123", "library_type": "user", "api_key": "new-key"}
    )

    assert store.get_tenant("123")["api_key"] == "new-key"


def test_tenant_api_key_is_encrypted_on_disk(tmp_path):
    path = str(tmp_path / "store.json")
    store = _store(path)
    store.put_tenant(
        "123", {"library_id": "123", "library_type": "user", "api_key": "s3cr3t"}
    )

    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    assert raw["tenants"]["123"]["api_key"] != "s3cr3t"
    assert "s3cr3t" not in json.dumps(raw)


def test_tenant_decrypt_requires_matching_fernet_key(tmp_path):
    path = str(tmp_path / "store.json")
    _store(path).put_tenant(
        "123", {"library_id": "123", "library_type": "user", "api_key": "s3cr3t"}
    )

    other_key_store = _store(path, fernet_key=Fernet.generate_key().decode())
    with pytest.raises(InvalidToken):
        other_key_store.get_tenant("123")
