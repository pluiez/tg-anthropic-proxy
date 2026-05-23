import sqlite3

from shared.cache_db import SqliteByteCache


def _key(n: int) -> str:
    return f"{n:064x}"


def test_sqlite_cache_persists_entries(tmp_path) -> None:
    path = tmp_path / "cache.sqlite3"

    cache = SqliteByteCache(path, ttl_seconds=60, max_items=10, max_bytes=1000, now_ms_fn=lambda: 1000)
    assert cache.put(_key(1), b"value", cache_ts=1000) is True

    reopened = SqliteByteCache(path, ttl_seconds=60, max_items=10, max_bytes=1000, now_ms_fn=lambda: 1000)
    assert reopened.get(_key(1)) == b"value"


def test_sqlite_cache_touch_uses_max_last_accessed(tmp_path) -> None:
    now = 100_000
    path = tmp_path / "cache.sqlite3"
    cache = SqliteByteCache(path, ttl_seconds=10, max_items=10, max_bytes=1000, now_ms_fn=lambda: now)

    cache.put(_key(1), b"value", cache_ts=100_000)
    cache.touch_many([_key(1)], cache_ts=90_000)

    with sqlite3.connect(path) as conn:
        row = conn.execute(
            "SELECT last_accessed_at FROM cache_entries WHERE key = ?",
            (_key(1),),
        ).fetchone()
    assert row == (100_000,)

    cache.touch_many([_key(1)], cache_ts=105_000)
    with sqlite3.connect(path) as conn:
        row = conn.execute(
            "SELECT last_accessed_at FROM cache_entries WHERE key = ?",
            (_key(1),),
        ).fetchone()
    assert row == (105_000,)


def test_sqlite_cache_expires_by_last_accessed(tmp_path) -> None:
    now = 100_000
    cache = SqliteByteCache(
        tmp_path / "cache.sqlite3",
        ttl_seconds=10,
        max_items=10,
        max_bytes=1000,
        now_ms_fn=lambda: now,
    )
    cache.put(_key(1), b"value", cache_ts=100_000)

    now = 109_999
    assert cache.get(_key(1)) == b"value"

    now = 110_001
    assert cache.get(_key(1)) is None
    assert len(cache) == 0


def test_sqlite_cache_enforces_item_and_byte_limits(tmp_path) -> None:
    cache = SqliteByteCache(
        tmp_path / "cache.sqlite3",
        ttl_seconds=60,
        max_items=2,
        max_bytes=5,
        now_ms_fn=lambda: 1000,
    )

    assert cache.put(_key(1), b"aa", cache_ts=1000) is True
    assert cache.put(_key(2), b"bb", cache_ts=2000) is True
    assert cache.put(_key(3), b"cc", cache_ts=3000) is True

    assert cache.get(_key(1)) is None
    assert cache.get(_key(2)) == b"bb"
    assert cache.get(_key(3)) == b"cc"
    assert cache.put(_key(4), b"dddddd", cache_ts=4000) is False
