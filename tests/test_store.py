import os
import time

import pytest

from tuomin_gateway.schemas import MappingEntry
from tuomin_gateway.store import MappingStore


def _entries():
    return [MappingEntry(placeholder="<ORG_001>", label="ORG", original_value="秘密集团公司", text_hash="sha256:x")]


def test_save_load_round_trip(tmp_path):
    store = MappingStore(tmp_path, ttl_seconds=3600)
    store.save("t1", _entries())
    back = store.load("t1")
    assert back[0].original_value == "秘密集团公司"
    assert back[0].placeholder == "<ORG_001>"


def test_at_rest_payload_has_no_plaintext(tmp_path):
    store = MappingStore(tmp_path, ttl_seconds=3600)
    path = store.save("t1", _entries())
    raw = path.read_bytes()
    # Encrypted platform ciphers must never expose the secret in clear.
    if raw.startswith((b"DPAPI1\n", b"MACAES1\n")):
        assert "秘密集团公司".encode("utf-8") not in raw


def test_missing_key_raises(tmp_path):
    store = MappingStore(tmp_path, ttl_seconds=3600)
    with pytest.raises(FileNotFoundError):
        store.load("nope")


def test_ttl_expiry_and_purge(tmp_path):
    store = MappingStore(tmp_path, ttl_seconds=10)
    path = store.save("t1", _entries())
    old = time.time() - 100
    os.utime(path, (old, old))
    with pytest.raises(FileNotFoundError):
        store.load("t1")  # expired loads as missing (and is deleted)
    assert not path.exists()


def test_distinct_keys_do_not_collide_after_sanitization(tmp_path):
    # "app:1" and "app/1" used to sanitize to the same file "app1" → one would
    # overwrite the other's originals. Hashed filenames keep them separate.
    store = MappingStore(tmp_path, ttl_seconds=3600)
    store.save("app:1", [MappingEntry("<ORG_001>", "ORG", "甲公司", "sha256:a")])
    store.save("app/1", [MappingEntry("<ORG_001>", "ORG", "乙公司", "sha256:b")])
    assert store.load("app:1")[0].original_value == "甲公司"
    assert store.load("app/1")[0].original_value == "乙公司"


def test_purge_expired_counts(tmp_path):
    store = MappingStore(tmp_path, ttl_seconds=10)
    p1 = store.save("a", _entries())
    store.save("b", _entries())
    old = time.time() - 100
    os.utime(p1, (old, old))
    assert store.purge_expired() == 1
