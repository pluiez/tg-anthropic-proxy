# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install (editable)
pip install -e .

# Run the client proxy (machine A, exposes :8787 to Anthropic-compatible tools)
python -m client.main

# Run the server relay (machine B, connects to Anthropic upstream)
python -m server.main

# Run the optional cc_proxy sidecar (same machine as server)
python -m cc_proxy.main
python -m server.main --use-cc-proxy   # server forwards through cc_proxy instead of directly

# Tests
python -m pytest
python -m pytest tests/test_framing_dynamic_chunks.py  # single file

# Quick syntax check
python -m compileall client server shared cc_proxy
```

Copy `.env.example` to `.env` and fill in `BOT_A_TOKEN`, `BOT_B_TOKEN`, and `BRIDGE_CHAT_ID` before running anything.

## Architecture

This project tunnels Anthropic API HTTP requests through a Telegram group as the transport layer. Both sides run a `python-telegram-bot` polling loop in the same process as their HTTP server.

```
[Claude Code / AI client]
        │ HTTP POST /v1/messages
        ▼
[client/  — FastAPI on :8787]
   frames request → Telegram (BOT_A_TOKEN)
        │ Telegram group (BRIDGE_CHAT_ID)
        ▼
[server/  — python-telegram-bot polling]
   reassembles frames → HTTP POST to ANTHROPIC_BASE_URL
        │
        ▼ (optional)
[cc_proxy/ — FastAPI on :8790]          ← injects Claude Code headers
        │
        ▼
   api.anthropic.com
```

**client/** (`BOT_A_TOKEN`): FastAPI proxy. Accepts `/v1/{path}` requests, builds a JSON envelope `{path, headers, body}`, chunks and encodes it as gzip+base64 Telegram text frames, then streams the response back to the caller via SSE. `client/main.py` owns the HTTP server and the per-request `asyncio.Queue`; `client/tg_client.py` owns the Telegram bot.

**server/** (`BOT_B_TOKEN`): Telegram message handler. Reassembles incoming `req`/`req_end` frames into the envelope, forwards the request via `httpx` to `ANTHROPIC_BASE_URL`, and sends SSE chunks back as Telegram frames. `server/relay.py` contains all logic; `server/config.py` reads `ANTHROPIC_BASE_URL` and `CC_PROXY_*` env vars.

**cc_proxy/**: Optional sidecar that runs between the server and Anthropic. Its only job is to inject the `User-Agent` and version headers that Claude Code expects (`cc_proxy/headers.py`). The server health-checks it at startup with `--use-cc-proxy`.

**shared/**: Transport-neutral helpers used by both client and server.
- `framing.py`: `make_frame`/`parse_frame` (gzip+base64 JSON), `chunk_request_envelope` (binary search to fit frames within Telegram's 4096-char limit), `make_request_blob`/`decode_request_blob` (single-document transport for large requests).
- `cache_protocol.py`: Telegram bridge cache — replaces the large `tools` field and reusable `system`/`messages` content blocks with bare 64-hex sha256 refs in the envelope so repeated Claude Code turns cross the bridge cheaply.
- `cache_db.py`: Local SQLite TTL+size-bounded byte cache used independently by client and server. The server no longer sends `cache_ack`; if its DB misses a ref, the client can replay the full envelope.
- `cache_store.py`: Legacy in-memory TTL+size-bounded byte cache retained for tests/compatibility.
- `logging_utils.py`: `redact_headers` and `summarize_json_body` for safe structured logging.

## Frame protocol

All Telegram text messages are single JSON objects:

```json
{"v": 1, "rid": "r_<hex12>", "seq": <int>, "kind": "<kind>", ...}
```

| Direction | Kind | Notes |
|-----------|------|-------|
| client → server | `req` | chunked envelope payload (gzip+base64 in `data`) |
| client → server | `req_end` | signals last chunk |
| client → server | document | `req_blob` caption + gzip file (replaces many `req` frames) |
| server → client | `resp_chunk` | streamed response bytes |
| server → client | `resp_end` | response complete |
| server → client | `resp_error` | upstream or relay error |
| server → client | `cache_miss` | server lacks a ref key (client replays full envelope) |
| server → client | `cache_ack` | legacy frame ignored by new clients; new servers should not send it |

The client switches to document transport when a request would require ≥ `PROXY_TELEGRAM_DOCUMENT_CHUNK_THRESHOLD` text frames (default 4).

## Key constraints

- Both bots must be members of the same Telegram group (`BRIDGE_CHAT_ID`). The group must allow bots to read messages.
- `AIORateLimiter` is configured for `group_max_rate=3/s`. The server's response coalescer (`FLUSH_INTERVAL=1.0s`, `FLUSH_BYTES=3072`) batches SSE chunks to stay near this limit.
- The client routes `PROXY_BASE_URL/v1/v1/messages` → upstream `/v1/messages` to handle double-`/v1` from Claude Code's SDK when `ANTHROPIC_BASE_URL` already contains `/v1` (`client/routing.py`).
- The cache uses canonical JSON (sorted keys) for stable bare 64-hex sha256 hashes. Cache fields are configurable; the default set is `tools,system,messages`.

## Coding style

Python 3.11+, four-space indentation, type hints for public/cross-module functions. Reuse `shared/framing.py` for any tunnel frame changes — do not duplicate framing logic. Keep async helpers small.

## Commit style

Concise imperative subjects. Use `Tested:` / `Not-tested:` trailers in the body when relevant.
