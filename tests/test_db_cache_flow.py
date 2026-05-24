import sys
import types


class _DummyFilter:
    def __and__(self, other):
        return self

    def __invert__(self):
        return self


class _DummyFilters:
    TEXT = _DummyFilter()
    COMMAND = _DummyFilter()
    Document = types.SimpleNamespace(ALL=_DummyFilter())


class _DummyApplication:
    @classmethod
    def builder(cls):
        return cls()

    def token(self, _token):
        return self

    def rate_limiter(self, _limiter):
        return self

    def build(self):
        return self


class _DummyUpdate:
    ALL_TYPES = []


def _install_telegram_stub() -> None:
    telegram = types.ModuleType("telegram")
    telegram.InputFile = lambda payload, filename=None: (payload, filename)
    telegram.Update = _DummyUpdate

    telegram_ext = types.ModuleType("telegram.ext")
    telegram_ext.AIORateLimiter = lambda **kwargs: kwargs
    telegram_ext.Application = _DummyApplication
    telegram_ext.ContextTypes = types.SimpleNamespace(DEFAULT_TYPE=object)
    telegram_ext.MessageHandler = lambda *args, **kwargs: (args, kwargs)
    telegram_ext.filters = _DummyFilters

    sys.modules.setdefault("telegram", telegram)
    sys.modules.setdefault("telegram.ext", telegram_ext)


_install_telegram_stub()

import json

import pytest

from client import main as client_main
from server import relay
from shared.cache_db import SqliteByteCache
from shared.cache_protocol import CACHE_REF_KEY, parse_cache_fields


def _body() -> dict:
    return {
        "model": "claude-opus-4-7",
        "tools": [
            {"name": "tool_a", "description": "A" * 512, "input_schema": {"type": "object"}},
            {"name": "tool_b", "description": "B" * 512, "input_schema": {"type": "object"}},
        ],
        "system": [{"type": "text", "text": "system prompt " * 100}],
        "messages": [
            {"role": "user", "content": "first " * 200},
            {"role": "assistant", "content": "second " * 200},
            {"role": "user", "content": "third"},
        ],
    }


def _install_client_cache(monkeypatch, cache: SqliteByteCache, *, cache_ts: int = 1_779_600_000_000) -> None:
    monkeypatch.setattr(client_main, "CACHE_ENABLED", True)
    monkeypatch.setattr(client_main, "CACHE_MIN_BYTES", 1)
    monkeypatch.setattr(client_main, "CACHE_FIELDS", parse_cache_fields(None))
    monkeypatch.setattr(client_main, "_CACHE_DB", cache)
    monkeypatch.setattr(client_main, "now_epoch_ms", lambda: cache_ts)
    monkeypatch.setattr(cache, "_now_ms_fn", lambda: cache_ts)


def _install_server_cache(monkeypatch, cache: SqliteByteCache, *, cache_ts: int = 1_779_600_000_000) -> None:
    monkeypatch.setattr(relay, "_cache_enabled", True)
    monkeypatch.setattr(relay, "_cache_min_bytes", 1)
    monkeypatch.setattr(relay, "_cache_fields", parse_cache_fields(None))
    monkeypatch.setattr(relay, "_cache", cache)
    monkeypatch.setattr(cache, "_now_ms_fn", lambda: cache_ts)


def test_client_uses_local_db_hit_for_second_request(monkeypatch, tmp_path) -> None:
    cache = SqliteByteCache(tmp_path / "client.sqlite3", ttl_seconds=3600, max_items=100, max_bytes=1_000_000)
    _install_client_cache(monkeypatch, cache)
    body = json.dumps(_body()).encode("utf-8")

    full_first, initial_first, first_stats = client_main._build_request_envelopes("/v1/messages", {}, body)
    assert initial_first == full_first
    assert first_stats["cache_db_hits"] == 0
    assert first_stats["cache_db_new_entries"] > 0

    full_second, initial_second, second_stats = client_main._build_request_envelopes("/v1/messages", {}, body)
    assert initial_second != full_second
    assert second_stats["cache_db_hits"] > 0
    assert second_stats["cache_refs"] > 0

    envelope = json.loads(initial_second)
    assert envelope["cache_ts"] == 1_779_600_000_000
    tools_key = envelope["body_json"]["tools"][CACHE_REF_KEY]["key"]
    assert len(tools_key) == 64
    int(tools_key, 16)


def test_server_miss_then_full_replay_repairs_server_db(monkeypatch, tmp_path) -> None:
    client_cache = SqliteByteCache(tmp_path / "client.sqlite3", ttl_seconds=3600, max_items=100, max_bytes=1_000_000)
    server_cache = SqliteByteCache(tmp_path / "server.sqlite3", ttl_seconds=3600, max_items=100, max_bytes=1_000_000)
    _install_client_cache(monkeypatch, client_cache)
    _install_server_cache(monkeypatch, server_cache)
    original_body = _body()
    body = json.dumps(original_body).encode("utf-8")

    client_main._build_request_envelopes("/v1/messages", {}, body)
    full_envelope, optimized_envelope, _stats = client_main._build_request_envelopes("/v1/messages", {}, body)

    with pytest.raises(relay.RequestCacheMiss):
        relay._body_from_envelope("r_test", json.loads(optimized_envelope))

    full_decoded = json.loads(full_envelope)
    _body_bytes, body_obj, _used, cache_ts = relay._body_from_envelope("r_test", full_decoded)
    stored = relay._store_body_cache("r_test", "/v1/messages", body_obj, cache_ts=cache_ts)
    assert stored

    restored_bytes, restored_obj, used_keys, _cache_ts = relay._body_from_envelope(
        "r_test",
        json.loads(optimized_envelope),
    )
    assert json.loads(restored_bytes) == original_body
    assert restored_obj == original_body
    assert used_keys


def test_cache_miss_replay_limit_is_cumulative(monkeypatch) -> None:
    monkeypatch.setattr(client_main, "_CACHE_MISS_REPLAY_COUNT", 0)
    monkeypatch.setattr(client_main, "CACHE_CLIENT_HIT_SERVER_MISS_MAX_REPLAYS", 1)

    assert client_main._consume_cache_miss_replay_slot() == (True, 1, 1)
    assert client_main._consume_cache_miss_replay_slot() == (False, 2, 1)
