# tg-anthropic-proxy

Anthropic-compatible API proxy that tunnels requests through a Telegram bridge.

## Architecture

There are three optional runtime processes:

- `client`: local FastAPI endpoint used by Claude Code or another Anthropic-compatible client.
- `server`: Telegram relay that receives framed requests and forwards them to Anthropic or to `cc_proxy`.
- `cc_proxy`: optional sidecar that rewrites forwarded requests to look like Claude Code requests, then forwards them to the real `ANTHROPIC_BASE_URL`.

Request flow:

1. Claude Code sends an Anthropic API request to the client URL, for example `http://127.0.0.1:8787/v1/messages`.
2. `client` accepts the HTTP request, normalizes the Anthropic path, keeps only relay-safe headers, and wraps `path`, `headers`, and request body into an internal envelope.
3. `client` sends that envelope to the shared Telegram chat with bot A (`BOT_A_TOKEN`). Small requests use text frames. Large requests may use one compressed Telegram document fallback. Repeated Claude Code payload sections can be replaced with internal cache references before sending.
4. `server` listens to the same Telegram chat with bot B (`BOT_B_TOKEN`). It receives text frames or request documents, reconstructs the internal envelope, restores any cache references from its in-memory cache, and rebuilds the full Anthropic request body.
5. `server` forwards the reconstructed request to the configured upstream:
   - without `--use-cc-proxy`: directly to `.env` `ANTHROPIC_BASE_URL`;
   - with `--use-cc-proxy`: to the local `cc_proxy` endpoint defined by `CC_PROXY_HOST` / `CC_PROXY_PORT`.
6. If enabled, `cc_proxy` rewrites headers/fingerprint for Claude Code compatibility and forwards the request to the real `.env` `ANTHROPIC_BASE_URL`.

Response flow:

1. The upstream returns an SSE response to `server` (directly or through `cc_proxy`).
2. `server` coalesces SSE bytes and sends response frames back through Telegram using bot B.
3. `client` receives those response frames with bot A and streams the bytes back on the original HTTP response to Claude Code.

The Telegram bridge is therefore on the critical path in both directions: request upload from `client` to `server`, and response return from `server` to `client`.

## Optional `cc_proxy`

`cc_proxy` is a sidecar service, not a normal route on the existing client/server chain. The default server behavior remains direct forwarding to `ANTHROPIC_BASE_URL`.

Run the sidecar manually:

```bash
python -m cc_proxy.main
```

Then start the server with:

```bash
python -m server.main --use-cc-proxy
```

When `--use-cc-proxy` is set, the server checks `cc_proxy` health at startup and exits with a clear error if the sidecar is not running.

## Telegram Bridge Optimizations

Request-side optimizations:

- Dynamic text-frame packing validates the actual encoded Telegram message length instead of using only a fixed raw byte size.
- Large request envelopes can use a compressed Telegram document fallback to avoid many text messages and Telegram flood limits.
- Protocol-level cache references let repeated Claude Code `tools`, `system`, and `messages` prefixes cross the bridge as small sha256 refs after the server has cached them.
- The cache is in-memory and has a default TTL of 72 hours. If the server cache misses, the client automatically resends the full request envelope.

Response-side optimizations:

- The first upstream response chunk is flushed immediately so the HTTP client sees bytes early.
- Later SSE bytes are coalesced by time/size thresholds to reduce Telegram `sendMessage` calls.
- Response frames use dynamic encoded-length packing, matching the request-side safety check.
- Server logs include response flush and per-frame Telegram send timing for debugging slow or timed-out requests.

## Development

Install dependencies:

```bash
python -m pip install -e .
```

Run the client:

```bash
python -m client.main
```

Run the server:

```bash
python -m server.main
```

Run checks:

```bash
python -m compileall client server shared cc_proxy
python -m pytest
```
