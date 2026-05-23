import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any

from dotenv import load_dotenv

load_dotenv()

import uvicorn  # noqa: E402
from fastapi import FastAPI, Request, Response  # noqa: E402
from fastapi.responses import StreamingResponse  # noqa: E402

from client import tg_client  # noqa: E402
from client.routing import anthropic_v1_path  # noqa: E402
from shared.cache_protocol import (  # noqa: E402
    DEFAULT_CACHE_MAX_ITEMS,
    DEFAULT_CACHE_MIN_BYTES,
    DEFAULT_CACHE_TTL_SECONDS,
    compress_body_with_cache_refs,
    parse_cache_fields,
)
from shared.framing import (  # noqa: E402
    MAX_TEXT_FRAME_CHARS,
    chunk_request_envelope,
    coerce_text_frame_chars,
    make_frame,
    make_request_blob,
    new_request_id,
)
from shared.logging_utils import redact_headers, summarize_json_body  # noqa: E402

log = logging.getLogger(__name__)

PENDING: dict[str, asyncio.Queue] = {}
_END = object()

_RELAY_HEADERS = {
    "anthropic-version",
    "anthropic-beta",
    "x-api-key",
    "authorization",
    "content-type",
    "accept",
}


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# How long the SSE generator will wait for the next frame before giving up.
RESPONSE_TIMEOUT = float(os.getenv("PROXY_RESPONSE_TIMEOUT", "180"))
FRAME_MAX_CHARS = coerce_text_frame_chars(
    os.getenv("PROXY_TELEGRAM_FRAME_MAX_CHARS"),
    default=MAX_TEXT_FRAME_CHARS,
)
DOCUMENT_CHUNK_THRESHOLD = _int_env("PROXY_TELEGRAM_DOCUMENT_CHUNK_THRESHOLD", 4)
CACHE_ENABLED = _bool_env("PROXY_TELEGRAM_CACHE_ENABLED", True)
CACHE_TTL_SECONDS = _int_env("PROXY_TELEGRAM_CACHE_TTL_SECONDS", DEFAULT_CACHE_TTL_SECONDS)
CACHE_MIN_BYTES = _int_env("PROXY_TELEGRAM_CACHE_MIN_BYTES", DEFAULT_CACHE_MIN_BYTES)
CACHE_FIELDS = parse_cache_fields(os.getenv("PROXY_TELEGRAM_CACHE_FIELDS"))
CACHE_KNOWN_MAX_ITEMS = _int_env("PROXY_TELEGRAM_CACHE_KNOWN_MAX_ITEMS", DEFAULT_CACHE_MAX_ITEMS * 4)
_KNOWN_CACHE_KEYS: dict[str, float] = {}


class CacheMiss(RuntimeError):
    def __init__(self, keys: list[str]) -> None:
        self.keys = keys
        super().__init__("cache miss: " + ",".join(keys))


def _client_addr(request: Request) -> str:
    if request.client is None:
        return "unknown"
    return f"{request.client.host}:{request.client.port}"


def _cache_now() -> float:
    return time.monotonic()


def _prune_known_cache_keys() -> None:
    now = _cache_now()
    expired = [key for key, expires_at in _KNOWN_CACHE_KEYS.items() if expires_at <= now]
    for key in expired:
        _KNOWN_CACHE_KEYS.pop(key, None)
    while len(_KNOWN_CACHE_KEYS) > CACHE_KNOWN_MAX_ITEMS:
        oldest = min(_KNOWN_CACHE_KEYS, key=_KNOWN_CACHE_KEYS.get)
        _KNOWN_CACHE_KEYS.pop(oldest, None)


def _known_cache_keys() -> frozenset[str]:
    _prune_known_cache_keys()
    return frozenset(_KNOWN_CACHE_KEYS)


def _remember_cache_keys(keys: list[str]) -> None:
    if not CACHE_ENABLED or not keys:
        return
    expires_at = _cache_now() + max(1, CACHE_TTL_SECONDS)
    for key in keys:
        if isinstance(key, str) and key:
            _KNOWN_CACHE_KEYS[key] = expires_at
    _prune_known_cache_keys()
    log.info("client remembered cache keys count=%d known_keys=%d", len(keys), len(_KNOWN_CACHE_KEYS))


def _forget_cache_keys(keys: list[str]) -> None:
    for key in keys:
        _KNOWN_CACHE_KEYS.pop(key, None)
    if keys:
        log.info("client forgot cache-missed keys count=%d known_keys=%d", len(keys), len(_KNOWN_CACHE_KEYS))


def _make_envelope(upstream_path: str, headers: dict[str, str], *, body: str | None = None, body_json: Any = None) -> bytes:
    envelope: dict[str, Any] = {
        "path": upstream_path,
        "headers": headers,
    }
    if body_json is not None:
        envelope["body_json"] = body_json
    else:
        envelope["body"] = body or ""
    return json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _build_request_envelopes(
    upstream_path: str,
    headers: dict[str, str],
    body: bytes,
) -> tuple[bytes, bytes, dict[str, Any]]:
    body_text = body.decode("utf-8")
    full_envelope = _make_envelope(upstream_path, headers, body=body_text)
    stats: dict[str, Any] = {
        "cache_enabled": CACHE_ENABLED,
        "cache_fields": sorted(CACHE_FIELDS),
        "cache_candidates": 0,
        "cache_refs": 0,
        "messages_prefix_len": 0,
        "optimized_bytes": None,
    }

    if not CACHE_ENABLED or upstream_path != "/v1/messages":
        return full_envelope, full_envelope, stats

    try:
        body_obj = json.loads(body_text)
    except json.JSONDecodeError:
        return full_envelope, full_envelope, stats

    result = compress_body_with_cache_refs(
        body_obj,
        _known_cache_keys(),
        min_bytes=CACHE_MIN_BYTES,
        fields=CACHE_FIELDS,
    )
    if result is None:
        return full_envelope, full_envelope, stats

    stats.update(
        {
            "cache_candidates": len(result.candidates),
            "cache_refs": len(result.ref_keys),
            "messages_prefix_len": result.messages_prefix_len,
        }
    )
    if not result.ref_keys:
        return full_envelope, full_envelope, stats

    optimized_envelope = _make_envelope(upstream_path, headers, body_json=result.body)
    stats["optimized_bytes"] = len(optimized_envelope)
    if len(optimized_envelope) >= len(full_envelope):
        stats["cache_refs"] = 0
        return full_envelope, full_envelope, stats

    return full_envelope, optimized_envelope, stats


async def _send_envelope(rid: str, envelope: bytes, *, reason: str) -> None:
    chunks = chunk_request_envelope(envelope, rid, max_chars=FRAME_MAX_CHARS)
    total = len(chunks)
    use_document = DOCUMENT_CHUNK_THRESHOLD > 0 and total >= DOCUMENT_CHUNK_THRESHOLD
    log.info(
        "[%s] client sending request over telegram reason=%s envelope_bytes=%d chunks=%d frame_max_chars=%d document_threshold=%d use_document=%s",
        rid,
        reason,
        len(envelope),
        total,
        FRAME_MAX_CHARS,
        DOCUMENT_CHUNK_THRESHOLD,
        use_document,
    )

    if use_document:
        caption, blob = make_request_blob(rid, envelope)
        filename = f"tg-anthropic-{rid}.json.gz"
        log.info(
            "[%s] client sending request document reason=%s envelope_bytes=%d blob_bytes=%d caption_chars=%d filename=%s",
            rid,
            reason,
            len(envelope),
            len(blob),
            len(caption),
            filename,
        )
        await tg_client.send_document(caption, blob, filename)
        log.info("[%s] client sent request document reason=%s blob_bytes=%d", rid, reason, len(blob))
        return

    for seq, c in enumerate(chunks):
        log.debug("[%s] client send req chunk reason=%s seq=%d/%d bytes=%d", rid, reason, seq + 1, total, len(c))
        await tg_client.send_frame(make_frame(rid, seq, "req", data=c, total=total))
    await tg_client.send_frame(make_frame(rid, total, "req_end"))
    log.info("[%s] client sent request end reason=%s seq=%d", rid, reason, total)


async def _on_frame(frame: dict[str, Any]) -> None:
    rid = frame["rid"]
    kind = frame["kind"]
    if kind == "cache_ack":
        keys = [key for key in frame.get("keys", []) if isinstance(key, str)]
        log.info("[%s] client received cache ack keys=%d", rid, len(keys))
        _remember_cache_keys(keys)
        return

    q = PENDING.get(rid)
    if q is None:
        log.warning("[%s] client received frame for unknown request kind=%s", rid, kind)
        return

    if kind == "cache_miss":
        keys = [key for key in frame.get("keys", []) if isinstance(key, str)]
        log.warning("[%s] client received cache miss keys=%d", rid, len(keys))
        _forget_cache_keys(keys)
        q.put_nowait(CacheMiss(keys))
    elif kind == "resp_chunk":
        payload = frame["payload"]
        log.debug("[%s] client received response chunk seq=%s bytes=%d", rid, frame.get("seq"), len(payload))
        q.put_nowait(payload)
    elif kind == "resp_end":
        log.info("[%s] client received response end seq=%s", rid, frame.get("seq"))
        q.put_nowait(_END)
    elif kind == "resp_error":
        error = frame.get("error", "unknown upstream error")
        log.warning("[%s] client received response error seq=%s error=%s", rid, frame.get("seq"), error)
        q.put_nowait(RuntimeError(error))
    else:
        log.debug("[%s] client ignored frame kind=%s", rid, kind)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    log.info(
        "client starting bridge listener cache_enabled=%s cache_fields=%s cache_ttl_seconds=%d cache_min_bytes=%d",
        CACHE_ENABLED,
        sorted(CACHE_FIELDS),
        CACHE_TTL_SECONDS,
        CACHE_MIN_BYTES,
    )
    await tg_client.start(_on_frame)
    log.info("client bridge listener ready")
    try:
        yield
    finally:
        log.info("client stopping bridge listener")
        await tg_client.stop()


app = FastAPI(lifespan=lifespan)


@app.head("/")
@app.head("/v1")
@app.head("/v1/")
async def base_health(request: Request) -> Response:
    log.info("client health probe method=%s path=%s from=%s", request.method, request.url.path, _client_addr(request))
    return Response(status_code=200)


@app.post("/v1/{path:path}")
async def proxy(path: str, request: Request):
    rid = new_request_id()
    started = time.monotonic()
    body = await request.body()
    headers = {
        k: v for k, v in request.headers.items() if k.lower() in _RELAY_HEADERS
    }
    upstream_path = anthropic_v1_path(path)
    log.info(
        "[%s] client accepted request method=%s path=%s normalized_path=%s from=%s body=%s headers=%s",
        rid,
        request.method,
        request.url.path,
        upstream_path,
        _client_addr(request),
        summarize_json_body(body),
        redact_headers(headers),
    )

    full_envelope, initial_envelope, cache_stats = _build_request_envelopes(upstream_path, headers, body)
    log.info(
        "[%s] client cache planning full_envelope_bytes=%d initial_envelope_bytes=%d stats=%s",
        rid,
        len(full_envelope),
        len(initial_envelope),
        cache_stats,
    )

    q: asyncio.Queue = asyncio.Queue()
    PENDING[rid] = q

    await _send_envelope(
        rid,
        initial_envelope,
        reason="cache_ref" if initial_envelope != full_envelope else "full",
    )

    async def gen():
        response_chunks = 0
        response_bytes = 0
        replayed_full_after_cache_miss = False
        try:
            while True:
                try:
                    item = await asyncio.wait_for(q.get(), timeout=RESPONSE_TIMEOUT)
                except asyncio.TimeoutError:
                    elapsed = time.monotonic() - started
                    log.warning(
                        "[%s] client response timeout timeout=%.1fs chunks=%d bytes=%d elapsed=%.3fs",
                        rid,
                        RESPONSE_TIMEOUT,
                        response_chunks,
                        response_bytes,
                        elapsed,
                    )
                    err = {"type": "proxy_error", "message": "response timeout"}
                    yield f"event: error\ndata: {json.dumps(err)}\n\n".encode("utf-8")
                    return
                if isinstance(item, CacheMiss):
                    if replayed_full_after_cache_miss:
                        elapsed = time.monotonic() - started
                        log.warning("[%s] client repeated cache miss keys=%s elapsed=%.3fs", rid, item.keys, elapsed)
                        err = {"type": "proxy_error", "message": "cache miss after full replay"}
                        yield f"event: error\ndata: {json.dumps(err)}\n\n".encode("utf-8")
                        return
                    replayed_full_after_cache_miss = True
                    log.info("[%s] client replaying full request after cache miss keys=%s", rid, item.keys)
                    await _send_envelope(rid, full_envelope, reason="cache_miss_replay")
                    continue
                if item is _END:
                    elapsed = time.monotonic() - started
                    log.info(
                        "[%s] client completed response chunks=%d bytes=%d elapsed=%.3fs",
                        rid,
                        response_chunks,
                        response_bytes,
                        elapsed,
                    )
                    return
                if isinstance(item, Exception):
                    elapsed = time.monotonic() - started
                    log.warning(
                        "[%s] client forwarding response error chunks=%d bytes=%d elapsed=%.3fs error=%s",
                        rid,
                        response_chunks,
                        response_bytes,
                        elapsed,
                        item,
                    )
                    err = {"type": "proxy_error", "message": str(item)}
                    yield f"event: error\ndata: {json.dumps(err)}\n\n".encode("utf-8")
                    return
                response_chunks += 1
                response_bytes += len(item)
                log.debug("[%s] client streaming response chunk=%d bytes=%d", rid, response_chunks, len(item))
                yield item
        finally:
            PENDING.pop(rid, None)

    return StreamingResponse(gen(), media_type="text/event-stream")


if __name__ == "__main__":
    uvicorn.run(
        app,
        host=os.getenv("PROXY_HOST", "127.0.0.1"),
        port=int(os.getenv("PROXY_PORT", "8787")),
    )
