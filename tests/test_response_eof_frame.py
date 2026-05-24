import asyncio
import sys
import types

from shared.framing import make_frame, parse_frame


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


class _FakeResponseStream:
    def __init__(self, pieces: list[bytes]) -> None:
        self.status_code = 200
        self.headers: dict[str, str] = {}
        self._pieces = pieces

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def aiter_bytes(self):
        for piece in self._pieces:
            yield piece

    async def aread(self) -> bytes:
        return b""


class _FakeHttpClient:
    def __init__(self, pieces: list[bytes]) -> None:
        self._pieces = pieces

    def stream(self, *_args, **_kwargs):
        return _FakeResponseStream(self._pieces)


def _response_envelope() -> bytes:
    return b'{"path":"/v1/messages","headers":{},"body":"{}"}'


def _run_process_with_pieces(monkeypatch, pieces: list[bytes]) -> list[dict]:
    sent: list[dict] = []

    async def fake_send(text: str) -> None:
        frame = parse_frame(text)
        assert frame is not None
        sent.append(frame)

    monkeypatch.setattr(relay, "_http", _FakeHttpClient(pieces))
    monkeypatch.setattr(relay, "_send", fake_send)
    monkeypatch.setattr(relay, "_anthropic_base", "https://upstream.test")
    monkeypatch.setattr(relay, "_use_cc_proxy", False)
    monkeypatch.setattr(relay, "_cache_enabled", False)
    monkeypatch.setattr(relay, "_cache", None)
    monkeypatch.setattr(relay, "RESPONSE_FLUSH_BYTES", 1_000_000)
    monkeypatch.setattr(relay, "RESPONSE_FLUSH_INTERVAL", 1_000_000.0)

    asyncio.run(relay._process("r_eof", _response_envelope()))
    return sent


def test_client_resp_chunk_eof_yields_payload_before_end() -> None:
    rid = "r_client_eof"
    queue: asyncio.Queue = asyncio.Queue()
    client_main.PENDING[rid] = queue
    frame = parse_frame(make_frame(rid, 0, "resp_chunk", data=b"final bytes", eof=True))

    assert frame is not None
    asyncio.run(client_main._on_frame(frame))

    assert queue.get_nowait() == b"final bytes"
    assert queue.get_nowait() is client_main._END
    client_main.PENDING.pop(rid, None)


def test_client_resp_chunk_empty_eof_ends_stream_without_extra_yield() -> None:
    rid = "r_client_empty_eof"
    queue: asyncio.Queue = asyncio.Queue()
    client_main.PENDING[rid] = queue
    frame = parse_frame(make_frame(rid, 0, "resp_chunk", data=b"", eof=True))

    assert frame is not None
    asyncio.run(client_main._on_frame(frame))

    assert queue.get_nowait() is client_main._END
    assert queue.empty()
    client_main.PENDING.pop(rid, None)


def test_client_still_accepts_legacy_resp_end() -> None:
    rid = "r_client_legacy_end"
    queue: asyncio.Queue = asyncio.Queue()
    client_main.PENDING[rid] = queue
    frame = parse_frame(make_frame(rid, 1, "resp_end"))

    assert frame is not None
    asyncio.run(client_main._on_frame(frame))

    assert queue.get_nowait() is client_main._END
    client_main.PENDING.pop(rid, None)


def test_server_merges_eof_into_final_payload_chunk(monkeypatch) -> None:
    sent = _run_process_with_pieces(monkeypatch, [b"first", b"final"])

    assert [frame["kind"] for frame in sent] == ["resp_chunk", "resp_chunk"]
    assert b"".join(frame["payload"] for frame in sent) == b"firstfinal"
    assert sent[0].get("eof") is None
    assert sent[1]["eof"] is True


def test_server_sends_empty_eof_chunk_when_final_buffer_is_empty(monkeypatch) -> None:
    sent = _run_process_with_pieces(monkeypatch, [b"first"])

    assert [frame["kind"] for frame in sent] == ["resp_chunk", "resp_chunk"]
    assert sent[0]["payload"] == b"first"
    assert sent[0].get("eof") is None
    assert sent[1]["payload"] == b""
    assert sent[1]["eof"] is True
