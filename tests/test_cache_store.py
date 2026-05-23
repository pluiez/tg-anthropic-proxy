from shared.cache_store import TtlByteCache


def test_ttl_byte_cache_expires_items() -> None:
    now = 100.0
    cache = TtlByteCache(ttl_seconds=10, max_items=10, max_bytes=100, time_fn=lambda: now)

    assert cache.put("a", b"abc") is True
    assert cache.get("a") == b"abc"

    now = 111.0
    assert cache.get("a") is None
    assert len(cache) == 0


def test_ttl_byte_cache_evicts_by_item_and_byte_limits() -> None:
    cache = TtlByteCache(ttl_seconds=100, max_items=2, max_bytes=5, time_fn=lambda: 0.0)

    assert cache.put("a", b"aa") is True
    assert cache.put("b", b"bb") is True
    assert cache.put("c", b"cc") is True

    assert cache.get("a") is None
    assert cache.get("b") == b"bb"
    assert cache.get("c") == b"cc"

    assert cache.put("d", b"dddddd") is False
    assert cache.get("d") is None


def test_put_many_returns_only_keys_still_retained() -> None:
    cache = TtlByteCache(ttl_seconds=100, max_items=2, max_bytes=100, time_fn=lambda: 0.0)

    stored = cache.put_many([("a", b"a"), ("b", b"b"), ("c", b"c")])

    assert stored == ["b", "c"]
    assert cache.get("a") is None
    assert cache.get("b") == b"b"
    assert cache.get("c") == b"c"
