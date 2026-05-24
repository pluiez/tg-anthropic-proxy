import asyncio
import json
import sys
import types

from shared.framing import (
    MAX_TEXT_FRAME_CHARS,
    chunk_request_envelope,
    make_frame,
    make_request_blob,
    parse_frame,
    parse_request_blob_caption,
)


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


def _reset_relay_state(rid: str) -> None:
    relay._PARTS.pop(rid, None)
    relay._TOTAL.pop(rid, None)
    relay._REQUEST_STARTED.pop(rid, None)


def _process_calls(monkeypatch) -> list[tuple[str, bytes]]:
    calls: list[tuple[str, bytes]] = []

    async def fake_process(rid: str, body: bytes) -> None:
        calls.append((rid, body))

    monkeypatch.setattr(relay, "_process", fake_process)
    return calls


async def _drain_pending_tasks() -> None:
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


def _capture_sent(monkeypatch) -> list[dict]:
    sent: list[dict] = []

    async def fake_send_frame(text: str) -> None:
        frame = parse_frame(text)
        assert frame is not None
        sent.append(frame)

    async def fake_send_document(caption: str, payload: bytes, filename: str) -> None:
        sent.append(
            {
                "kind": "document",
                "caption": caption,
                "payload": payload,
                "filename": filename,
            }
        )

    monkeypatch.setattr(client_main.tg_client, "send_frame", fake_send_frame)
    monkeypatch.setattr(client_main.tg_client, "send_document", fake_send_document)
    return sent


def test_chunk_request_envelope_reserves_space_for_eof_on_last_frame() -> None:
    import random

    rid = "r_eofchunk0"
    # Use random bytes so gzip cannot compress the payload into a single frame.
    rng = random.Random(0)
    data = bytes(rng.randrange(256) for _ in range(40_000))

    chunks = chunk_request_envelope(rid=rid, envelope=data, last_extra={"eof": True})
    total = len(chunks)
    assert total > 1
    assert b"".join(chunks) == data

    for seq, chunk in enumerate(chunks):
        extra = {"total": total}
        if seq == total - 1:
            extra["eof"] = True
        frame = make_frame(rid, seq, "req", data=chunk, **extra)
        assert len(frame) <= MAX_TEXT_FRAME_CHARS


def test_client_text_path_emits_eof_and_no_req_end(monkeypatch) -> None:
    monkeypatch.setattr(client_main, "DOCUMENT_CHUNK_THRESHOLD", 0)
    sent = _capture_sent(monkeypatch)
    envelope = json.dumps({"path": "/v1/messages", "headers": {}, "body": "hi"}).encode("utf-8")

    asyncio.run(client_main._send_envelope("r_clienteof0", envelope, reason="full"))

    kinds = [frame.get("kind") for frame in sent]
    assert "req_end" not in kinds
    assert kinds[-1] == "req"
    assert sent[-1]["eof"] is True
    assert sent[-1].get("total") == len(sent)
    for frame in sent[:-1]:
        assert frame.get("eof") is None


def test_client_text_path_single_frame_marks_eof(monkeypatch) -> None:
    monkeypatch.setattr(client_main, "DOCUMENT_CHUNK_THRESHOLD", 0)
    sent = _capture_sent(monkeypatch)
    envelope = b'{"path":"/v1/messages","headers":{},"body":"hi"}'

    asyncio.run(client_main._send_envelope("r_singleeof", envelope, reason="full"))

    assert len(sent) == 1
    assert sent[0]["kind"] == "req"
    assert sent[0]["seq"] == 0
    assert sent[0]["eof"] is True
    assert sent[0]["total"] == 1


def test_client_document_fallback_unaffected_by_eof(monkeypatch) -> None:
    monkeypatch.setattr(client_main, "DOCUMENT_CHUNK_THRESHOLD", 1)
    sent = _capture_sent(monkeypatch)
    envelope = json.dumps({"path": "/v1/messages", "headers": {}, "body": "x" * 2000}).encode("utf-8")

    asyncio.run(client_main._send_envelope("r_doceof", envelope, reason="full"))

    assert len(sent) == 1
    document = sent[0]
    assert document["kind"] == "document"
    metadata = parse_request_blob_caption(document["caption"])
    assert metadata is not None
    assert metadata["rid"] == "r_doceof"
    assert "eof" not in metadata


def test_server_reassembles_request_from_req_eof_frame(monkeypatch) -> None:
    rid = "r_servereof0"
    _reset_relay_state(rid)
    calls = _process_calls(monkeypatch)
    envelope = json.dumps({"path": "/v1/messages", "headers": {}, "body": "hi"}).encode("utf-8")
    chunks = chunk_request_envelope(rid=rid, envelope=envelope, last_extra={"eof": True})
    total = len(chunks)

    async def feed() -> None:
        for seq, chunk in enumerate(chunks):
            extra: dict = {"total": total}
            if seq == total - 1:
                extra["eof"] = True
            frame = parse_frame(make_frame(rid, seq, "req", data=chunk, **extra))
            assert frame is not None
            await relay._on_req_frame(frame)
        await _drain_pending_tasks()

    asyncio.run(feed())

    assert calls == [(rid, envelope)]
    assert rid not in relay._PARTS
    assert rid not in relay._TOTAL


def test_server_handles_eof_arriving_before_earlier_frames(monkeypatch) -> None:
    import random

    rid = "r_servereofooo"
    _reset_relay_state(rid)
    calls = _process_calls(monkeypatch)
    # High-entropy payload so chunking actually splits across frames.
    rng = random.Random(1)
    envelope = bytes(rng.randrange(256) for _ in range(8_000))
    chunks = chunk_request_envelope(
        rid=rid,
        envelope=envelope,
        max_chars=1024,
        last_extra={"eof": True},
    )
    total = len(chunks)
    assert total >= 3

    async def feed_out_of_order() -> None:
        last_seq = total - 1
        last_frame = parse_frame(
            make_frame(
                rid,
                last_seq,
                "req",
                data=chunks[last_seq],
                total=total,
                eof=True,
            )
        )
        assert last_frame is not None
        finalize_task = asyncio.create_task(relay._on_req_frame(last_frame))
        await asyncio.sleep(0.01)
        for seq in range(last_seq):
            frame = parse_frame(make_frame(rid, seq, "req", data=chunks[seq], total=total))
            assert frame is not None
            await relay._on_req_frame(frame)
        await finalize_task
        await _drain_pending_tasks()

    asyncio.run(feed_out_of_order())

    assert calls == [(rid, envelope)]
    assert rid not in relay._PARTS
    assert rid not in relay._TOTAL


def test_server_still_finalizes_on_legacy_req_end(monkeypatch) -> None:
    rid = "r_legacyend0"
    _reset_relay_state(rid)
    calls = _process_calls(monkeypatch)
    envelope = json.dumps({"path": "/v1/messages", "headers": {}, "body": "legacy"}).encode("utf-8")
    chunks = chunk_request_envelope(rid=rid, envelope=envelope)
    total = len(chunks)

    async def feed_legacy() -> None:
        for seq, chunk in enumerate(chunks):
            frame = parse_frame(make_frame(rid, seq, "req", data=chunk, total=total))
            assert frame is not None
            await relay._on_req_frame(frame)
        end_frame = parse_frame(make_frame(rid, total, "req_end"))
        assert end_frame is not None
        await relay._on_req_frame(end_frame)
        await _drain_pending_tasks()

    asyncio.run(feed_legacy())

    assert calls == [(rid, envelope)]
    assert rid not in relay._PARTS
    assert rid not in relay._TOTAL


def test_server_skips_duplicate_finalize_when_eof_and_req_end_both_arrive(monkeypatch) -> None:
    rid = "r_dupfinalize"
    _reset_relay_state(rid)
    calls = _process_calls(monkeypatch)
    envelope = b'{"path":"/v1/messages","headers":{},"body":"dup"}'
    chunks = chunk_request_envelope(rid=rid, envelope=envelope, last_extra={"eof": True})
    total = len(chunks)

    async def feed_both() -> None:
        for seq, chunk in enumerate(chunks):
            extra: dict = {"total": total}
            if seq == total - 1:
                extra["eof"] = True
            frame = parse_frame(make_frame(rid, seq, "req", data=chunk, **extra))
            assert frame is not None
            await relay._on_req_frame(frame)
        # Simulate an older client that also sends a trailing req_end frame.
        end_frame = parse_frame(make_frame(rid, total, "req_end"))
        assert end_frame is not None
        await relay._on_req_frame(end_frame)
        await _drain_pending_tasks()

    asyncio.run(feed_both())

    assert calls == [(rid, envelope)]


def test_server_request_document_blob_path_unchanged(monkeypatch) -> None:
    rid = "r_docunchanged"
    _reset_relay_state(rid)
    calls = _process_calls(monkeypatch)
    envelope = json.dumps({"path": "/v1/messages", "headers": {}, "body": "doc"}).encode("utf-8")
    caption, blob = make_request_blob(rid, envelope)
    metadata = parse_request_blob_caption(caption)
    assert metadata is not None

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

    async def run_document() -> bool:
        handled = await relay._on_req_document(msg)
        await _drain_pending_tasks()
        return handled

    handled = asyncio.run(run_document())
    assert handled is True
    assert calls == [(rid, envelope)]
    assert sent_frames == []
