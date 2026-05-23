import asyncio
import json
import logging
import os
import time
from collections import defaultdict
from typing import Any, Optional

import httpx
from telegram import Update
from telegram.ext import (
    AIORateLimiter,
    Application,
    ContextTypes,
    MessageHandler,
    filters,
)

from server.config import cc_proxy_base_url, configured_anthropic_base_url
from shared.cache_protocol import (
    DEFAULT_CACHE_MAX_BYTES,
    DEFAULT_CACHE_MAX_ITEMS,
    DEFAULT_CACHE_MIN_BYTES,
    DEFAULT_CACHE_TTL_SECONDS,
    body_json_bytes,
    cache_candidates_for_body,
    parse_cache_fields,
    restore_body_from_cache_refs,
)
from shared.cache_db import SqliteByteCache, now_epoch_ms
from shared.framing import (
    MAX_TEXT_FRAME_CHARS,
    chunk_bytes_for_frame_payloads,
    coerce_text_frame_chars,
    decode_request_blob,
    make_frame,
    parse_frame,
    parse_request_blob_caption,
)
from shared.logging_utils import redact_headers, summarize_json_body

# Response coalescer defaults. The first upstream chunk is flushed
# immediately to satisfy clients waiting for first bytes; subsequent chunks are
# coalesced to reduce Telegram sendMessage count and flood-limit exposure.
DEFAULT_RESPONSE_FLUSH_INTERVAL = 2.0
DEFAULT_RESPONSE_FLUSH_BYTES = 8192
RESPONSE_FLUSH_INTERVAL = DEFAULT_RESPONSE_FLUSH_INTERVAL
RESPONSE_FLUSH_BYTES = DEFAULT_RESPONSE_FLUSH_BYTES
RESPONSE_FRAME_MAX_CHARS = MAX_TEXT_FRAME_CHARS

log = logging.getLogger(__name__)

_app: Optional[Application] = None
_http: Optional[httpx.AsyncClient] = None
_bridge_chat_id: Optional[int] = None
_anthropic_base: str = "https://api.anthropic.com"
_use_cc_proxy = False
_cache_enabled = True
_cache_min_bytes = DEFAULT_CACHE_MIN_BYTES
_cache_fields = parse_cache_fields(None)
_cache: SqliteByteCache | None = None


class CcProxyUnavailable(RuntimeError):
    pass


class RequestCacheMiss(RuntimeError):
    def __init__(self, keys: list[str]) -> None:
        self.keys = keys
        super().__init__("cache miss: " + ",".join(keys))


# Reassembly buffers for incoming request frames.
_PARTS: dict[str, dict[int, bytes]] = defaultdict(dict)
_TOTAL: dict[str, int] = {}
_REQUEST_STARTED: dict[str, float] = {}


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


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


async def _ensure_cc_proxy_healthy(base_url: str) -> None:
    health_url = f"{base_url.rstrip('/')}/health"
    log.info("server checking cc_proxy health url=%s", health_url)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=2.0)) as client:
            response = await client.get(health_url)
            response.raise_for_status()
            data = response.json()
    except Exception as exc:
        log.warning("server cc_proxy health check failed url=%s error=%s", health_url, exc)
        raise CcProxyUnavailable(
            "cc_proxy is not healthy at "
            f"{health_url}. Start it manually with `python -m cc_proxy.main` "
            "before running `python -m server.main --use-cc-proxy`."
        ) from exc
    if data.get("status") != "ok":
        log.warning("server cc_proxy health check returned unexpected data url=%s data=%s", health_url, data)
        raise CcProxyUnavailable(
            "cc_proxy health check returned an unexpected response from "
            f"{health_url}: {data!r}"
        )
    log.info("server cc_proxy health ok url=%s data=%s", health_url, data)


async def _send(text: str) -> None:
    assert _app is not None and _bridge_chat_id is not None
    await _app.bot.send_message(
        chat_id=_bridge_chat_id,
        text=text,
        disable_notification=True,
    )


def _cache_get(key: str) -> bytes | None:
    if not _cache_enabled or _cache is None:
        return None
    return _cache.get(key)


def _cache_ts_from_envelope(envelope: dict[str, Any]) -> int:
    raw = envelope.get("cache_ts")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        try:
            return int(raw)
        except ValueError:
            pass
    return now_epoch_ms()


def _store_body_cache(rid: str, path: str, body_obj: Any, *, cache_ts: int) -> list[str]:
    if not _cache_enabled or _cache is None or path != "/v1/messages":
        return []
    candidates = cache_candidates_for_body(
        body_obj,
        min_bytes=_cache_min_bytes,
        fields=_cache_fields,
    )
    stored_keys = _cache.put_many(
        ((candidate.key, candidate.data) for candidate in candidates),
        cache_ts=cache_ts,
    )
    if candidates:
        log.info(
            "[%s] server cache candidates=%d newly_stored=%d cache_items=%d cache_bytes=%d fields=%s",
            rid,
            len(candidates),
            len(stored_keys),
            len(_cache),
            _cache.total_bytes,
            sorted(_cache_fields),
        )
    return stored_keys


def _body_from_envelope(rid: str, envelope: dict[str, Any]) -> tuple[bytes, Any, list[str], int] | None:
    cache_ts = _cache_ts_from_envelope(envelope)
    if "body_json" in envelope:
        body_obj = envelope["body_json"]
        restore = restore_body_from_cache_refs(body_obj, _cache_get)
        if restore.missing_keys:
            log.warning(
                "[%s] server cache miss missing_keys=%s used_keys=%s",
                rid,
                restore.missing_keys,
                restore.used_keys,
            )
            raise RequestCacheMiss(restore.missing_keys)
        if restore.used_keys:
            if _cache is not None:
                _cache.touch_many(restore.used_keys, cache_ts=cache_ts)
            log.info("[%s] server restored cache refs used_keys=%d", rid, len(restore.used_keys))
        return body_json_bytes(restore.body), restore.body, restore.used_keys, cache_ts

    body_text = envelope.get("body", "")
    body = str(body_text).encode("utf-8")
    try:
        body_obj = json.loads(body)
    except Exception:
        body_obj = None
    return body, body_obj, [], cache_ts


async def _on_req_document(msg) -> bool:
    document = msg.document
    metadata = parse_request_blob_caption(msg.caption)
    if document is None or metadata is None:
        return False

    rid = metadata["rid"]
    started = time.monotonic()
    _REQUEST_STARTED[rid] = started
    log.info(
        "[%s] server received request document file_id=%s file_name=%s file_size=%s caption=%s",
        rid,
        document.file_id,
        document.file_name,
        document.file_size,
        metadata,
    )
    try:
        telegram_file = await document.get_file(read_timeout=30.0, connect_timeout=10.0)
        blob = bytes(
            await telegram_file.download_as_bytearray(
                read_timeout=60.0,
                connect_timeout=10.0,
            )
        )
        raw = decode_request_blob(blob, metadata)
    except Exception as exc:
        _REQUEST_STARTED.pop(rid, None)
        log.warning("[%s] server failed to read request document error=%s", rid, exc)
        await _send(make_frame(rid, 0, "resp_error", error=f"bad request document: {exc}"))
        return True

    log.info(
        "[%s] server decoded request document envelope_bytes=%d blob_bytes=%d elapsed=%.3fs",
        rid,
        len(raw),
        len(blob),
        time.monotonic() - started,
    )
    _PARTS.pop(rid, None)
    _TOTAL.pop(rid, None)
    asyncio.create_task(_process(rid, raw))
    return True


async def _on_req_frame(frame: dict) -> None:
    rid = frame["rid"]
    kind = frame["kind"]
    if kind == "req":
        payload = frame["payload"]
        _REQUEST_STARTED.setdefault(rid, time.monotonic())
        _PARTS[rid][frame["seq"]] = payload
        if "total" in frame:
            _TOTAL[rid] = int(frame["total"])
        log.debug(
            "[%s] server received req frame seq=%s total=%s bytes=%d collected=%d",
            rid,
            frame.get("seq"),
            frame.get("total"),
            len(payload),
            len(_PARTS[rid]),
        )
    elif kind == "req_end":
        started = _REQUEST_STARTED.setdefault(rid, time.monotonic())
        expected = _TOTAL.get(rid)
        collected = len(_PARTS.get(rid, {}))
        log.info(
            "[%s] server received req_end seq=%s expected_chunks=%s collected_chunks=%d",
            rid,
            frame.get("seq"),
            expected,
            collected,
        )
        # Wait briefly for any req frames that arrived out of order.
        for _ in range(40):
            if rid in _TOTAL and len(_PARTS[rid]) >= _TOTAL[rid]:
                break
            await asyncio.sleep(0.05)
        parts = _PARTS.pop(rid, {})
        expected = _TOTAL.pop(rid, None)
        if expected is not None and len(parts) < expected:
            missing = sorted(set(range(expected)) - set(parts))
            log.warning(
                "[%s] server processing incomplete request expected_chunks=%d collected_chunks=%d missing=%s",
                rid,
                expected,
                len(parts),
                missing,
            )
        body = b"".join(parts[i] for i in sorted(parts))
        log.info(
            "[%s] server reassembled request envelope_bytes=%d elapsed=%.3fs",
            rid,
            len(body),
            time.monotonic() - started,
        )
        asyncio.create_task(_process(rid, body))
    else:
        log.debug("[%s] server ignored frame kind=%s", rid, kind)


async def _process(rid: str, raw: bytes) -> None:
    started = _REQUEST_STARTED.pop(rid, time.monotonic())
    try:
        envelope = json.loads(raw.decode("utf-8"))
    except Exception as e:
        log.warning("[%s] server bad envelope bytes=%d error=%s", rid, len(raw), e)
        await _send(make_frame(rid, 0, "resp_error", error=f"bad envelope: {e}"))
        return

    path = envelope["path"]
    url = f"{_anthropic_base.rstrip('/')}{path}"
    # Force identity encoding: some upstream proxies gzip SSE responses, which
    # breaks streaming semantics and pushes opaque binary bytes through the
    # tunnel. aiter_bytes() below is also a safety net if upstream ignores us.
    headers = {**envelope.get("headers", {}), "accept-encoding": "identity"}
    if _use_cc_proxy:
        headers["x-tg-proxy-rid"] = rid

    try:
        body_result = _body_from_envelope(rid, envelope)
    except RequestCacheMiss as miss:
        await _send(make_frame(rid, 0, "cache_miss", keys=miss.keys))
        return
    except Exception as exc:
        log.warning("[%s] server failed to decode cached envelope error=%s", rid, exc)
        await _send(make_frame(rid, 0, "resp_error", error=f"bad cached envelope: {exc}"))
        return

    body, body_obj, _used_cache_keys, cache_ts = body_result
    stored_cache_keys = _store_body_cache(rid, path, body_obj, cache_ts=cache_ts)
    log.info(
        "[%s] server forwarding request upstream=%s use_cc_proxy=%s body=%s headers=%s",
        rid,
        url,
        _use_cc_proxy,
        summarize_json_body(body),
        redact_headers(headers),
    )

    seq = 0
    buf = bytearray()
    last_flush = time.monotonic()
    first_response_flush_done = False
    upstream_bytes = 0
    telegram_chunks = 0

    async def flush(reason: str) -> None:
        nonlocal seq, last_flush, telegram_chunks
        if not buf:
            return
        data = bytes(buf)
        buf.clear()
        chunks = chunk_bytes_for_frame_payloads(
            data,
            rid,
            "resp_chunk",
            max_chars=RESPONSE_FRAME_MAX_CHARS,
            start_seq=seq,
        )
        log.info(
            "[%s] server flushing response reason=%s bytes=%d telegram_frames=%d next_seq=%d frame_max_chars=%d",
            rid,
            reason,
            len(data),
            len(chunks),
            seq,
            RESPONSE_FRAME_MAX_CHARS,
        )
        for chunk in chunks:
            frame = make_frame(rid, seq, "resp_chunk", data=chunk)
            frame_chars = len(frame)
            send_started = time.monotonic()
            log.info(
                "[%s] server sending response frame reason=%s seq=%d bytes=%d frame_chars=%d",
                rid,
                reason,
                seq,
                len(chunk),
                frame_chars,
            )
            await _send(frame)
            log.info(
                "[%s] server sent response frame reason=%s seq=%d bytes=%d frame_chars=%d elapsed=%.3fs",
                rid,
                reason,
                seq,
                len(chunk),
                frame_chars,
                time.monotonic() - send_started,
            )
            telegram_chunks += 1
            seq += 1
        last_flush = time.monotonic()

    try:
        assert _http is not None
        async with _http.stream("POST", url, headers=headers, content=body) as resp:
            log.info(
                "[%s] server upstream response status=%d headers=%s",
                rid,
                resp.status_code,
                redact_headers(resp.headers),
            )
            if resp.status_code >= 400:
                err = (await resp.aread())[:1000].decode("utf-8", "ignore")
                log.warning("[%s] server upstream error status=%d body_prefix=%r", rid, resp.status_code, err)
                await _send(
                    make_frame(
                        rid,
                        seq,
                        "resp_error",
                        error=f"upstream {resp.status_code}: {err}",
                    )
                )
                return
            async for piece in resp.aiter_bytes():
                upstream_bytes += len(piece)
                buf.extend(piece)
                log.debug("[%s] server upstream chunk bytes=%d buffered=%d", rid, len(piece), len(buf))
                if not first_response_flush_done:
                    await flush("first_chunk")
                    first_response_flush_done = True
                elif (
                    len(buf) >= RESPONSE_FLUSH_BYTES
                    or (time.monotonic() - last_flush) >= RESPONSE_FLUSH_INTERVAL
                ):
                    await flush("interval_or_size")
            await flush("eof")
    except Exception as e:
        log.exception("[%s] server relay error upstream=%s", rid, url)
        await _send(make_frame(rid, seq, "resp_error", error=str(e)))
        return

    end_frame = make_frame(rid, seq, "resp_end")
    end_started = time.monotonic()
    await _send(end_frame)
    log.info(
        "[%s] server sent response end seq=%d frame_chars=%d elapsed=%.3fs",
        rid,
        seq,
        len(end_frame),
        time.monotonic() - end_started,
    )
    log.info(
        "[%s] server completed response upstream_bytes=%d telegram_chunks=%d end_seq=%d elapsed=%.3fs stored_cache_keys=%d used_cache_keys=%d",
        rid,
        upstream_bytes,
        telegram_chunks,
        seq,
        time.monotonic() - started,
        len(stored_cache_keys),
        len(_used_cache_keys),
    )


async def _handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg:
        return
    if msg.chat_id != _bridge_chat_id:
        log.debug("server ignored message from chat_id=%s", msg.chat_id)
        return
    if msg.document is not None and await _on_req_document(msg):
        return
    if not msg.text:
        return
    frame = parse_frame(msg.text)
    if frame is not None and frame["kind"] in ("req", "req_end"):
        await _on_req_frame(frame)


async def serve(*, use_cc_proxy: bool = False) -> None:
    global _app, _http, _bridge_chat_id, _anthropic_base, _use_cc_proxy
    global _cache_enabled, _cache_min_bytes, _cache_fields, _cache
    global RESPONSE_FLUSH_INTERVAL, RESPONSE_FLUSH_BYTES, RESPONSE_FRAME_MAX_CHARS
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    _use_cc_proxy = use_cc_proxy
    if use_cc_proxy:
        _anthropic_base = cc_proxy_base_url()
        await _ensure_cc_proxy_healthy(_anthropic_base)
    else:
        _anthropic_base = configured_anthropic_base_url()

    RESPONSE_FLUSH_INTERVAL = max(0.1, _float_env("PROXY_RESPONSE_FLUSH_INTERVAL", DEFAULT_RESPONSE_FLUSH_INTERVAL))
    RESPONSE_FLUSH_BYTES = max(1, _int_env("PROXY_RESPONSE_FLUSH_BYTES", DEFAULT_RESPONSE_FLUSH_BYTES))
    RESPONSE_FRAME_MAX_CHARS = coerce_text_frame_chars(
        os.getenv("PROXY_TELEGRAM_RESPONSE_FRAME_MAX_CHARS") or os.getenv("PROXY_TELEGRAM_FRAME_MAX_CHARS"),
        default=MAX_TEXT_FRAME_CHARS,
    )

    _cache_enabled = _bool_env("PROXY_TELEGRAM_CACHE_ENABLED", True)
    _cache_min_bytes = _int_env("PROXY_TELEGRAM_CACHE_MIN_BYTES", DEFAULT_CACHE_MIN_BYTES)
    _cache_fields = parse_cache_fields(os.getenv("PROXY_TELEGRAM_CACHE_FIELDS"))
    if _cache_enabled:
        _cache = SqliteByteCache(
            os.getenv("PROXY_SERVER_CACHE_DB_PATH", ".cache/server-cache.sqlite3"),
            ttl_seconds=_int_env("PROXY_TELEGRAM_CACHE_TTL_SECONDS", DEFAULT_CACHE_TTL_SECONDS),
            max_items=_int_env("PROXY_TELEGRAM_CACHE_MAX_ITEMS", DEFAULT_CACHE_MAX_ITEMS),
            max_bytes=_int_env("PROXY_TELEGRAM_CACHE_MAX_BYTES", DEFAULT_CACHE_MAX_BYTES),
        )
    else:
        _cache = None

    _bridge_chat_id = int(os.environ["BRIDGE_CHAT_ID"])
    token = os.environ["BOT_B_TOKEN"]

    _http = httpx.AsyncClient(
        timeout=httpx.Timeout(600.0, connect=10.0),
        http2=True,
    )

    _app = (
        Application.builder()
        .token(token)
        .rate_limiter(
            AIORateLimiter(
                overall_max_rate=30,
                overall_time_period=1,
                group_max_rate=3,
                group_time_period=1,
                max_retries=5,
            )
        )
        .build()
    )
    _app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _handler))
    _app.add_handler(MessageHandler(filters.Document.ALL, _handler))
    await _app.initialize()
    await _app.start()
    await _app.updater.start_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )

    log.info(
        "Server up on bridge chat %s, upstream %s, use_cc_proxy=%s cache_enabled=%s cache_fields=%s cache_min_bytes=%d response_flush_interval=%.3f response_flush_bytes=%d response_frame_max_chars=%d",
        _bridge_chat_id,
        _anthropic_base,
        _use_cc_proxy,
        _cache_enabled,
        sorted(_cache_fields),
        _cache_min_bytes,
        RESPONSE_FLUSH_INTERVAL,
        RESPONSE_FLUSH_BYTES,
        RESPONSE_FRAME_MAX_CHARS,
    )
    try:
        await asyncio.Event().wait()
    finally:
        log.info("server shutting down")
        if _app.updater is not None:
            await _app.updater.stop()
        await _app.stop()
        await _app.shutdown()
        await _http.aclose()
