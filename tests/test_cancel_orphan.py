import asyncio
import json
import sys
import types

import pytest

from shared.framing import make_frame, parse_frame, make_request_blob


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

from client import main as client_main
from server import relay


def _reset_server_state(rid: str) -> None:
    relay._PARTS.pop(rid, None)
    relay._TOTAL.pop(rid, None)
    relay._REQUEST_STARTED.pop(rid, None)
    relay._CANCELLED.pop(rid, None)
    task = relay._TASKS.pop(rid, None)
    if task is not None and not task.done():
        task.cancel()


# ---------------------------------------------------------------------------
# Client-side: PENDING cleanup and cancel emission
# ---------------------------------------------------------------------------


def test_proxy_endpoint_send_failure_returns_502_cleans_pending_and_emits_cancel(monkeypatch) -> None:
    from fastapi.testclient import TestClient

    async def _noop_start(_on_frame):
        return None

    async def _noop_stop():
        return None

    sent_frames: list[dict] = []

    async def fake_send_frame(text: str) -> None:
        frame = parse_frame(text)
        assert frame is not None
        sent_frames.append(frame)

    async def failing_send_envelope(rid: str, envelope: bytes, *, reason: str) -> None:
        raise TimeoutError("simulated sendDocument timeout")

    monkeypatch.setattr(client_main.tg_client, "start", _noop_start)
    monkeypatch.setattr(client_main.tg_client, "stop", _noop_stop)
    monkeypatch.setattr(client_main.tg_client, "send_frame", fake_send_frame)
    monkeypatch.setattr(client_main, "_send_envelope", failing_send_envelope)

    pending_before = set(client_main.PENDING.keys())

    with TestClient(client_main.app) as test_client:
        resp = test_client.post("/v1/messages", json={"model": "x"})

    assert resp.status_code == 502
    payload = resp.json()
    assert payload["type"] == "proxy_error"
    assert "telegram bridge send failed" in payload["message"]

    # No new entries leaked into PENDING.
    assert set(client_main.PENDING.keys()) == pending_before

    # Cancel frames are emitted via asyncio.create_task during the request;
    # because lifespan tears down the loop on exit, the task may or may not
    # have completed before exit. In this test, the direct call to
    # _send_cancel_best_effort happens in the same coroutine context as the
    # failure handler, so it runs before the response is returned.
    cancel_frames = [f for f in sent_frames if f.get("kind") == "cancel"]
    assert any(f["reason"] == "send_envelope_failed" for f in cancel_frames), (
        f"expected a send_envelope_failed cancel frame, saw {cancel_frames}"
    )


def test_send_cancel_best_effort_swallows_send_errors(monkeypatch) -> None:
    async def boom(text: str) -> None:
        raise RuntimeError("telegram unreachable")

    monkeypatch.setattr(client_main.tg_client, "send_frame", boom)
    # Should not raise even if send_frame errors.
    asyncio.run(client_main._send_cancel_best_effort("r_swallow", reason="probe"))


# ---------------------------------------------------------------------------
# Server-side: cancel before document/text drops them
# ---------------------------------------------------------------------------


def test_server_drops_document_when_cancel_arrived_first(monkeypatch) -> None:
    rid = "r_canceldoc"
    _reset_server_state(rid)
    process_calls: list[tuple[str, bytes]] = []

    async def fake_process(rid_: str, body: bytes) -> None:
        process_calls.append((rid_, body))

    monkeypatch.setattr(relay, "_process", fake_process)
    monkeypatch.setattr(relay, "CANCEL_TTL_SECONDS", 60.0)

    envelope = b'{"path":"/v1/messages","headers":{},"body":"drop me"}'
    caption, blob = make_request_blob(rid, envelope)

    class _FakeFile:
        async def download_as_bytearray(self, **kwargs):
            return bytearray(blob)

    class _FakeDocument:
        file_id = "fid"
        file_name = "fn"
        file_size = len(blob)

        async def get_file(self, **kwargs):  # noqa: D401 - test stub
            return _FakeFile()

    sent_frames: list[str] = []

    async def fake_send(text: str) -> None:
        sent_frames.append(text)

    monkeypatch.setattr(relay, "_send", fake_send)

    cancel_frame = parse_frame(make_frame(rid, 0, "cancel", reason="client_gave_up"))
    assert cancel_frame is not None
    msg = types.SimpleNamespace(document=_FakeDocument(), caption=caption)

    async def run() -> bool:
        await relay._on_cancel(cancel_frame)
        return await relay._on_req_document(msg)

    handled = asyncio.run(run())
    assert handled is True
    assert process_calls == []
    assert rid not in relay._TASKS
    assert sent_frames == []
    _reset_server_state(rid)


def test_server_drops_text_frames_for_cancelled_rid(monkeypatch) -> None:
    rid = "r_canceltext"
    _reset_server_state(rid)
    process_calls: list[tuple[str, bytes]] = []

    async def fake_process(rid_: str, body: bytes) -> None:
        process_calls.append((rid_, body))

    monkeypatch.setattr(relay, "_process", fake_process)
    monkeypatch.setattr(relay, "CANCEL_TTL_SECONDS", 60.0)

    async def run() -> None:
        cancel = parse_frame(make_frame(rid, 0, "cancel", reason="client_gave_up"))
        assert cancel is not None
        await relay._on_cancel(cancel)

        for seq in range(2):
            frame = parse_frame(make_frame(rid, seq, "req", data=b"x", total=2))
            assert frame is not None
            await relay._on_req_frame(frame)

        eof = parse_frame(make_frame(rid, 2, "req", data=b"y", total=3, eof=True))
        assert eof is not None
        await relay._on_req_frame(eof)

    asyncio.run(run())
    assert process_calls == []
    assert rid not in relay._PARTS
    assert rid not in relay._TOTAL
    _reset_server_state(rid)


# ---------------------------------------------------------------------------
# Server-side: cancel during processing aborts upstream and stops response
# ---------------------------------------------------------------------------


def test_cancel_during_processing_stops_response_send(monkeypatch) -> None:
    rid = "r_canceldur"
    _reset_server_state(rid)
    monkeypatch.setattr(relay, "CANCEL_TTL_SECONDS", 60.0)

    sent_after_cancel: list[str] = []
    sent_before_cancel_count = 0
    cancel_event = asyncio.Event()

    class _SlowStream:
        status_code = 200
        headers: dict[str, str] = {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def aiter_bytes(self):
            # First yield triggers cancel, second yield would only run if
            # cancellation failed to interrupt the streaming loop.
            yield b"first chunk"
            cancel_event.set()
            await asyncio.sleep(0.5)
            yield b"would-be second chunk"

        async def aread(self) -> bytes:
            return b""

    class _SlowClient:
        def stream(self, *_args, **_kwargs):
            return _SlowStream()

    monkeypatch.setattr(relay, "_http", _SlowClient())
    monkeypatch.setattr(relay, "_anthropic_base", "https://upstream.test")
    monkeypatch.setattr(relay, "_use_cc_proxy", False)
    monkeypatch.setattr(relay, "_cache_enabled", False)
    monkeypatch.setattr(relay, "_cache", None)
    monkeypatch.setattr(relay, "RESPONSE_FLUSH_BYTES", 1)
    monkeypatch.setattr(relay, "RESPONSE_FLUSH_INTERVAL", 0.01)

    cancel_seen = False

    async def fake_send(text: str) -> None:
        nonlocal cancel_seen, sent_before_cancel_count
        frame = parse_frame(text)
        assert frame is not None
        if cancel_seen:
            sent_after_cancel.append(text)
        else:
            sent_before_cancel_count += 1

    monkeypatch.setattr(relay, "_send", fake_send)

    async def run() -> None:
        nonlocal cancel_seen
        envelope = json.dumps({"path": "/v1/messages", "headers": {}, "body": "{}"}).encode()
        task = asyncio.create_task(relay._process(rid, envelope))
        relay._TASKS[rid] = task
        await cancel_event.wait()
        cancel_seen = True
        cancel_frame = parse_frame(make_frame(rid, 0, "cancel", reason="client_gave_up"))
        assert cancel_frame is not None
        await relay._on_cancel(cancel_frame)
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())

    # First chunk was flushed (one or more resp_chunk frames) before cancel.
    assert sent_before_cancel_count >= 1
    # After cancel, _process must not emit any further frames.
    assert sent_after_cancel == []
    assert rid not in relay._TASKS
    _reset_server_state(rid)


# ---------------------------------------------------------------------------
# Server-side: normal document path still works and cleans up _TASKS
# ---------------------------------------------------------------------------


def test_normal_document_path_unaffected_and_cleans_tasks(monkeypatch) -> None:
    rid = "r_normaldoc"
    _reset_server_state(rid)

    process_calls: list[tuple[str, bytes]] = []
    process_done = asyncio.Event()

    async def fake_process(rid_: str, body: bytes) -> None:
        process_calls.append((rid_, body))
        # Simulate the relay's own finally that pops _TASKS.
        relay._TASKS.pop(rid_, None)
        process_done.set()

    monkeypatch.setattr(relay, "_process", fake_process)

    envelope = json.dumps({"path": "/v1/messages", "headers": {}, "body": "normal"}).encode()
    caption, blob = make_request_blob(rid, envelope)

    class _FakeFile:
        async def download_as_bytearray(self, **kwargs):
            return bytearray(blob)

    class _FakeDocument:
        file_id = "fid"
        file_name = "fn"
        file_size = len(blob)

        async def get_file(self, **kwargs):
            return _FakeFile()

    sent_frames: list[str] = []

    async def fake_send(text: str) -> None:
        sent_frames.append(text)

    monkeypatch.setattr(relay, "_send", fake_send)

    msg = types.SimpleNamespace(document=_FakeDocument(), caption=caption)

    async def run() -> bool:
        handled = await relay._on_req_document(msg)
        await process_done.wait()
        return handled

    handled = asyncio.run(run())
    assert handled is True
    assert process_calls == [(rid, envelope)]
    assert rid not in relay._TASKS
    assert sent_frames == []
    _reset_server_state(rid)


# ---------------------------------------------------------------------------
# _CANCELLED TTL pruning
# ---------------------------------------------------------------------------


def test_cancelled_entries_expire_after_ttl(monkeypatch) -> None:
    rid = "r_canceltl"
    _reset_server_state(rid)
    monkeypatch.setattr(relay, "CANCEL_TTL_SECONDS", 0.0)

    asyncio.run(relay._on_cancel({"rid": rid, "kind": "cancel", "reason": "test"}))
    # TTL is zero, so the next is_cancelled check should prune the entry.
    assert relay._is_cancelled(rid) is False
    assert rid not in relay._CANCELLED


# ---------------------------------------------------------------------------
# Client tg_client document timeouts read from env at start()
# ---------------------------------------------------------------------------


def test_tg_client_document_timeouts_read_from_env(monkeypatch) -> None:
    from client import tg_client

    monkeypatch.setenv("PROXY_TELEGRAM_DOCUMENT_CONNECT_TIMEOUT", "3")
    monkeypatch.setenv("PROXY_TELEGRAM_DOCUMENT_WRITE_TIMEOUT", "45")
    monkeypatch.setenv("PROXY_TELEGRAM_DOCUMENT_READ_TIMEOUT", "55")
    monkeypatch.setenv("PROXY_TELEGRAM_DOCUMENT_POOL_TIMEOUT", "7")
    monkeypatch.setenv("BRIDGE_CHAT_ID", "0")
    monkeypatch.setenv("BOT_A_TOKEN", "x")

    # Drive the same logic that `start()` runs, without standing up a real bot.
    timeouts = {
        "connect_timeout": tg_client._float_env("PROXY_TELEGRAM_DOCUMENT_CONNECT_TIMEOUT", 10.0),
        "write_timeout": tg_client._float_env("PROXY_TELEGRAM_DOCUMENT_WRITE_TIMEOUT", 60.0),
        "read_timeout": tg_client._float_env("PROXY_TELEGRAM_DOCUMENT_READ_TIMEOUT", 60.0),
        "pool_timeout": tg_client._float_env("PROXY_TELEGRAM_DOCUMENT_POOL_TIMEOUT", 10.0),
    }
    assert timeouts == {
        "connect_timeout": 3.0,
        "write_timeout": 45.0,
        "read_timeout": 55.0,
        "pool_timeout": 7.0,
    }
