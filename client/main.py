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


# How long the SSE generator will wait for the next frame before giving up.
RESPONSE_TIMEOUT = float(os.getenv("PROXY_RESPONSE_TIMEOUT", "180"))
FRAME_MAX_CHARS = coerce_text_frame_chars(
    os.getenv("PROXY_TELEGRAM_FRAME_MAX_CHARS"),
    default=MAX_TEXT_FRAME_CHARS,
)
DOCUMENT_CHUNK_THRESHOLD = _int_env("PROXY_TELEGRAM_DOCUMENT_CHUNK_THRESHOLD", 4)


def _client_addr(request: Request) -> str:
    if request.client is None:
        return "unknown"
    return f"{request.client.host}:{request.client.port}"


async def _on_frame(frame: dict[str, Any]) -> None:
    rid = frame["rid"]
    q = PENDING.get(rid)
    if q is None:
        log.warning("[%s] client received frame for unknown request kind=%s", rid, frame.get("kind"))
        return
    kind = frame["kind"]
    if kind == "resp_chunk":
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
    log.info("client starting bridge listener")
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

    envelope = json.dumps(
        {
            "path": upstream_path,
            "headers": headers,
            "body": body.decode("utf-8"),
        }
    ).encode("utf-8")
    chunks = chunk_request_envelope(envelope, rid, max_chars=FRAME_MAX_CHARS)
    total = len(chunks)
    use_document = DOCUMENT_CHUNK_THRESHOLD > 0 and total >= DOCUMENT_CHUNK_THRESHOLD
    log.info(
        "[%s] client sending request over telegram envelope_bytes=%d chunks=%d frame_max_chars=%d document_threshold=%d use_document=%s",
        rid,
        len(envelope),
        total,
        FRAME_MAX_CHARS,
        DOCUMENT_CHUNK_THRESHOLD,
        use_document,
    )

    q: asyncio.Queue = asyncio.Queue()
    PENDING[rid] = q

    if use_document:
        caption, blob = make_request_blob(rid, envelope)
        filename = f"tg-anthropic-{rid}.json.gz"
        log.info(
            "[%s] client sending request document envelope_bytes=%d blob_bytes=%d caption_chars=%d filename=%s",
            rid,
            len(envelope),
            len(blob),
            len(caption),
            filename,
        )
        await tg_client.send_document(caption, blob, filename)
        log.info("[%s] client sent request document blob_bytes=%d", rid, len(blob))
    else:
        for seq, c in enumerate(chunks):
            log.debug("[%s] client send req chunk seq=%d/%d bytes=%d", rid, seq + 1, total, len(c))
            await tg_client.send_frame(make_frame(rid, seq, "req", data=c, total=total))
        await tg_client.send_frame(make_frame(rid, total, "req_end"))
        log.info("[%s] client sent request end seq=%d", rid, total)

    async def gen():
        response_chunks = 0
        response_bytes = 0
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
