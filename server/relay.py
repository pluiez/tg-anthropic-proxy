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

from shared.framing import chunk_bytes, make_frame, parse_frame

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

# Reassembly buffers for incoming request frames.
_PARTS: dict[str, dict[int, bytes]] = defaultdict(dict)
_TOTAL: dict[str, int] = {}


async def _send(text: str) -> None:
    assert _app is not None and _bridge_chat_id is not None
    await _app.bot.send_message(
        chat_id=_bridge_chat_id,
        text=text,
        disable_notification=True,
    )


async def _on_req_frame(frame: dict) -> None:
    rid = frame["rid"]
    kind = frame["kind"]
    if kind == "req":
        _PARTS[rid][frame["seq"]] = frame["payload"]
        if "total" in frame:
            _TOTAL[rid] = int(frame["total"])
    elif kind == "req_end":
        # Wait briefly for any req frames that arrived out of order.
        for _ in range(40):
            if rid in _TOTAL and len(_PARTS[rid]) >= _TOTAL[rid]:
                break
            await asyncio.sleep(0.05)
        parts = _PARTS.pop(rid, {})
        _TOTAL.pop(rid, None)
        body = b"".join(parts[i] for i in sorted(parts))
        asyncio.create_task(_process(rid, body))


async def _process(rid: str, raw: bytes) -> None:
    try:
        envelope = json.loads(raw.decode("utf-8"))
    except Exception as e:
        await _send(make_frame(rid, 0, "resp_error", error=f"bad envelope: {e}"))
        return

    url = f"{_anthropic_base.rstrip('/')}{envelope['path']}"
    # Force identity encoding: some upstream proxies gzip SSE responses, which
    # breaks streaming semantics and pushes opaque binary bytes through the
    # tunnel. aiter_bytes() below is also a safety net if upstream ignores us.
    headers = {**envelope.get("headers", {}), "accept-encoding": "identity"}
    body = envelope.get("body", "").encode("utf-8")

    seq = 0
    buf = bytearray()
    last_flush = time.monotonic()

    async def flush() -> None:
        nonlocal seq, last_flush
        if not buf:
            return
        data = bytes(buf)
        buf.clear()
        last_flush = time.monotonic()
        for chunk in chunk_bytes(data):
            await _send(make_frame(rid, seq, "resp_chunk", data=chunk))
            seq += 1

    try:
        assert _http is not None
        async with _http.stream("POST", url, headers=headers, content=body) as resp:
            if resp.status_code >= 400:
                err = (await resp.aread())[:1000].decode("utf-8", "ignore")
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
                buf.extend(piece)
                if (
                    len(buf) >= FLUSH_BYTES
                    or (time.monotonic() - last_flush) >= FLUSH_INTERVAL
                ):
                    await flush()
            await flush()
    except Exception as e:
        log.exception("[%s] relay error", rid)
        await _send(make_frame(rid, seq, "resp_error", error=str(e)))
        return

    await _send(make_frame(rid, seq, "resp_end"))


async def _handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg or not msg.text or msg.chat_id != _bridge_chat_id:
        return
    frame = parse_frame(msg.text)
    if frame is not None and frame["kind"] in ("req", "req_end"):
        await _on_req_frame(frame)


async def serve() -> None:
    global _app, _http, _bridge_chat_id, _anthropic_base
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    _bridge_chat_id = int(os.environ["BRIDGE_CHAT_ID"])
    _anthropic_base = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
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
    await _app.initialize()
    await _app.start()
    await _app.updater.start_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )

    log.info("Server up on bridge chat %s, upstream %s", _bridge_chat_id, _anthropic_base)
    try:
        await asyncio.Event().wait()
    finally:
        if _app.updater is not None:
            await _app.updater.stop()
        await _app.stop()
        await _app.shutdown()
        await _http.aclose()
