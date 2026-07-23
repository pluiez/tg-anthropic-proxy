import argparse
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager

from dotenv import load_dotenv

load_dotenv()

import httpx  # noqa: E402
import uvicorn  # noqa: E402
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import PlainTextResponse, Response, StreamingResponse  # noqa: E402

from cc_proxy.headers import build_claude_code_headers, build_passthrough_headers  # noqa: E402
from shared.logging_utils import redact_headers, summarize_json_body  # noqa: E402

log = logging.getLogger(__name__)

_RESPONSE_DROP_HEADERS = {
    "connection",
    "content-encoding",
    "content-length",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


def _upstream_base_url() -> str:
    configured = os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com").strip()
    return (configured or "https://api.anthropic.com").rstrip("/")


def _join_upstream_url(base_url: str, request: Request) -> str:
    url = f"{base_url}{request.url.path}"
    if request.url.query:
        url = f"{url}?{request.url.query}"
    return url


def _response_headers(headers: httpx.Headers) -> dict[str, str]:
    return {
        name: value
        for name, value in headers.items()
        if name.lower() not in _RESPONSE_DROP_HEADERS
    }


def _request_id(request: Request) -> str:
    return request.headers.get("x-tg-proxy-rid") or f"ccp_{uuid.uuid4().hex[:12]}"


async def _stream_response(
    rid: str,
    upstream_response: httpx.Response,
    *,
    method: str,
    upstream_url: str,
    started: float,
):
    chunks = 0
    total_bytes = 0
    try:
        async for piece in upstream_response.aiter_bytes():
            chunks += 1
            total_bytes += len(piece)
            log.debug("[%s] cc_proxy upstream stream chunk=%d bytes=%d", rid, chunks, len(piece))
            yield piece
    except Exception:
        log.exception("[%s] cc_proxy stream failed method=%s upstream=%s", rid, method, upstream_url)
        raise
    finally:
        await upstream_response.aclose()
        log.info(
            "[%s] cc_proxy stream closed status=%d chunks=%d bytes=%d elapsed=%.3fs",
            rid,
            upstream_response.status_code,
            chunks,
            total_bytes,
            time.monotonic() - started,
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    app.state.upstream_base_url = _upstream_base_url()
    app.state.http = httpx.AsyncClient(
        timeout=httpx.Timeout(600.0, connect=10.0),
        http2=True,
    )
    log.info("cc_proxy up, forwarding to %s", app.state.upstream_base_url)
    try:
        yield
    finally:
        log.info("cc_proxy shutting down")
        await app.state.http.aclose()


app = FastAPI(lifespan=lifespan)
app.state.claude_code_headers_enabled = True


@app.get("/health")
async def health(request: Request):
    log.info("cc_proxy health probe from=%s", request.client.host if request.client else "unknown")
    return {
        "status": "ok",
        "upstream_base_url": request.app.state.upstream_base_url,
    }


@app.head("/")
async def root_head(request: Request) -> Response:
    log.info("cc_proxy root HEAD probe from=%s", request.client.host if request.client else "unknown")
    return Response(status_code=204)


@app.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
)
async def proxy(path: str, request: Request):
    del path
    rid = _request_id(request)
    started = time.monotonic()
    body = await request.body()
    upstream_url = _join_upstream_url(request.app.state.upstream_base_url, request)
    incoming_headers = redact_headers(request.headers)
    claude_code_headers_enabled = getattr(request.app.state, "claude_code_headers_enabled", True)
    header_mode = "claude-code" if claude_code_headers_enabled else "passthrough"
    if claude_code_headers_enabled:
        headers = build_claude_code_headers(request.headers)
    else:
        headers = build_passthrough_headers(request.headers)
    log.info(
        "[%s] cc_proxy accepted request method=%s path=%s upstream=%s header_mode=%s body=%s incoming_headers=%s",
        rid,
        request.method,
        request.url.path,
        upstream_url,
        header_mode,
        summarize_json_body(body),
        incoming_headers,
    )
    log.info("[%s] cc_proxy forwarding headers=%s", rid, redact_headers(headers))

    upstream_request = request.app.state.http.build_request(
        request.method,
        upstream_url,
        headers=headers,
        content=body,
    )
    try:
        upstream_response = await request.app.state.http.send(upstream_request, stream=True)
    except httpx.TimeoutException as exc:
        log.warning(
            "[%s] cc_proxy upstream timeout method=%s upstream=%s elapsed=%.3fs error=%s",
            rid,
            request.method,
            upstream_url,
            time.monotonic() - started,
            exc,
        )
        return PlainTextResponse("upstream timeout", status_code=504)
    except httpx.HTTPError as exc:
        log.warning(
            "[%s] cc_proxy upstream request failed method=%s upstream=%s elapsed=%.3fs error=%s",
            rid,
            request.method,
            upstream_url,
            time.monotonic() - started,
            exc,
        )
        return PlainTextResponse(f"upstream request failed: {exc}", status_code=502)

    response_headers = _response_headers(upstream_response.headers)
    log.info(
        "[%s] cc_proxy upstream response status=%d headers=%s elapsed_to_headers=%.3fs",
        rid,
        upstream_response.status_code,
        redact_headers(response_headers),
        time.monotonic() - started,
    )

    if upstream_response.status_code >= 400:
        error_body = await upstream_response.aread()
        await upstream_response.aclose()
        log.warning(
            "[%s] cc_proxy upstream error status=%d body_prefix=%r elapsed=%.3fs",
            rid,
            upstream_response.status_code,
            error_body[:1000].decode("utf-8", "ignore"),
            time.monotonic() - started,
        )
        return Response(
            content=error_body,
            status_code=upstream_response.status_code,
            headers=response_headers,
        )

    return StreamingResponse(
        _stream_response(
            rid,
            upstream_response,
            method=request.method,
            upstream_url=upstream_url,
            started=started,
        ),
        status_code=upstream_response.status_code,
        headers=response_headers,
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the optional Anthropic cc_proxy sidecar.")
    parser.add_argument(
        "--no-claude-code-headers",
        action="store_true",
        help="forward request headers as-is except proxy/internal hop-by-hop headers",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    app.state.claude_code_headers_enabled = not args.no_claude_code_headers
    uvicorn.run(
        app,
        host=os.getenv("CC_PROXY_HOST", "127.0.0.1"),
        port=int(os.getenv("CC_PROXY_PORT", "8790")),
    )
