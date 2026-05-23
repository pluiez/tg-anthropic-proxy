import asyncio
import json
import logging
import os
import time
from collections import defaultdict
from typing import Optional

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
from shared.framing import (
    chunk_bytes,
    decode_request_blob,
    make_frame,
    parse_frame,
    parse_request_blob_caption,
)
from shared.logging_utils import redact_headers, summarize_json_body

# Coalescer thresholds:
#   - 1 second since last flush, OR
#   - 3072 bytes accumulated (≈ 75% of Telegram's 4096-char text limit)
# Either trigger flushes the buffer. Picked to minimise message rate so we
# stay close to AIORateLimiter's group_max_rate of 3/sec.
FLUSH_INTERVAL = 1.0
FLUSH_BYTES = 3072

log = logging.getLogger(__name__)

_app: Optional[Application] = None
_http: Optional[httpx.AsyncClient] = None
_bridge_chat_id: Optional[int] = None
_anthropic_base: str = "https://api.anthropic.com"
_use_cc_proxy = False


class CcProxyUnavailable(RuntimeError):
    pass


# Reassembly buffers for incoming request frames.
_PARTS: dict[str, dict[int, bytes]] = defaultdict(dict)
_TOTAL: dict[str, int] = {}
_REQUEST_STARTED: dict[str, float] = {}


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
    body = envelope.get("body", "").encode("utf-8")
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
    upstream_bytes = 0
    telegram_chunks = 0

    async def flush() -> None:
        nonlocal seq, last_flush, telegram_chunks
        if not buf:
            return
        data = bytes(buf)
        buf.clear()
        last_flush = time.monotonic()
        chunks = chunk_bytes(data)
        log.debug(
            "[%s] server flushing response bytes=%d telegram_chunks=%d next_seq=%d",
            rid,
            len(data),
            len(chunks),
            seq,
        )
        for chunk in chunks:
            await _send(make_frame(rid, seq, "resp_chunk", data=chunk))
            telegram_chunks += 1
            seq += 1

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
                if (
                    len(buf) >= FLUSH_BYTES
                    or (time.monotonic() - last_flush) >= FLUSH_INTERVAL
                ):
                    await flush()
            await flush()
    except Exception as e:
        log.exception("[%s] server relay error upstream=%s", rid, url)
        await _send(make_frame(rid, seq, "resp_error", error=str(e)))
        return

    await _send(make_frame(rid, seq, "resp_end"))
    log.info(
        "[%s] server completed response upstream_bytes=%d telegram_chunks=%d end_seq=%d elapsed=%.3fs",
        rid,
        upstream_bytes,
        telegram_chunks,
        seq,
        time.monotonic() - started,
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
        "Server up on bridge chat %s, upstream %s, use_cc_proxy=%s",
        _bridge_chat_id,
        _anthropic_base,
        _use_cc_proxy,
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
