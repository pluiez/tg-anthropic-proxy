import asyncio
import json
import os
from contextlib import asynccontextmanager
from typing import Any

from dotenv import load_dotenv

load_dotenv()

import uvicorn  # noqa: E402
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import StreamingResponse  # noqa: E402

from client import tg_client  # noqa: E402
from shared.framing import chunk_bytes, make_frame, new_request_id  # noqa: E402

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

# How long the SSE generator will wait for the next frame before giving up.
RESPONSE_TIMEOUT = float(os.getenv("PROXY_RESPONSE_TIMEOUT", "180"))


async def _on_frame(frame: dict[str, Any]) -> None:
    rid = frame["rid"]
    q = PENDING.get(rid)
    if q is None:
        return
    kind = frame["kind"]
    if kind == "resp_chunk":
        q.put_nowait(frame["payload"])
    elif kind == "resp_end":
        q.put_nowait(_END)
    elif kind == "resp_error":
        q.put_nowait(RuntimeError(frame.get("error", "unknown upstream error")))


@asynccontextmanager
async def lifespan(app: FastAPI):
    await tg_client.start(_on_frame)
    try:
        yield
    finally:
        await tg_client.stop()


app = FastAPI(lifespan=lifespan)


@app.post("/v1/{path:path}")
async def proxy(path: str, request: Request):
    rid = new_request_id()
    body = await request.body()
    headers = {
        k: v for k, v in request.headers.items() if k.lower() in _RELAY_HEADERS
    }
    envelope = json.dumps(
        {
            "path": f"/v1/{path}",
            "headers": headers,
            "body": body.decode("utf-8"),
        }
    ).encode("utf-8")
    chunks = chunk_bytes(envelope)
    total = len(chunks)

    q: asyncio.Queue = asyncio.Queue()
    PENDING[rid] = q

    for seq, c in enumerate(chunks):
        await tg_client.send_frame(make_frame(rid, seq, "req", data=c, total=total))
    await tg_client.send_frame(make_frame(rid, total, "req_end"))

    async def gen():
        try:
            while True:
                item = await asyncio.wait_for(q.get(), timeout=RESPONSE_TIMEOUT)
                if item is _END:
                    return
                if isinstance(item, Exception):
                    err = {"type": "proxy_error", "message": str(item)}
                    yield (
                        f"event: error\ndata: {json.dumps(err)}\n\n"
                    ).encode("utf-8")
                    return
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
