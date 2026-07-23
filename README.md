# tg-anthropic-proxy

Anthropic-compatible API proxy that tunnels requests through a Telegram bridge.

## Setup: bots and bridge channel

The bridge requires two Telegram bots and one shared Telegram **channel** (not a group — group transport is not used here). Steps below assume the official Telegram client and `curl`.

1. Create two bots in [@BotFather](https://t.me/BotFather):
   - Send `/newbot` twice. The first bot becomes bot A (used by `client`, token goes into `BOT_A_TOKEN`); the second becomes bot B (used by `server`, token goes into `BOT_B_TOKEN`).
   - Bot privacy mode does not affect channel posts, so no `/setprivacy` change is required.

2. Create a private channel in Telegram (any name; channel mode, not group), then add both bots as **administrators**:
   - Channel → Manage Channel → Administrators → Add Admin → search the bot username → grant at minimum "Post Messages" and "Delete Messages". Repeat for the other bot.
   - Both bots must be admins of the same channel; otherwise neither side can read or send frames.

3. Discover the channel ID (`BRIDGE_CHAT_ID`). Channel IDs are negative integers of the form `-100xxxxxxxxxx`. Pick one method:
   - Post any message in the channel, then call `https://api.telegram.org/bot<BOT_A_TOKEN>/getUpdates` from a browser or `curl`. Look for `channel_post.chat.id`. If the field is missing, ensure the bot was added as admin *before* the post and that no other long-poller is consuming updates.
   - Or forward a channel message to [@userinfobot](https://t.me/userinfobot) / [@RawDataBot](https://t.me/RawDataBot) and copy the reported chat id.

4. Fill `.env` (copy from `.env.example`):
   ```
   BOT_A_TOKEN=<token from step 1, bot A>
   BOT_B_TOKEN=<token from step 1, bot B>
   BRIDGE_CHAT_ID=-100xxxxxxxxxx
   ```
   The same `.env` shape is used on both the client machine and the server machine; only `BOT_A_TOKEN` is read by `client`, only `BOT_B_TOKEN` by `server`, and both read `BRIDGE_CHAT_ID`.

5. Sanity-check by starting `python -m server.main` and `python -m client.main` on their respective hosts and watching the logs for the first request to round-trip through the channel.

`clear_channel.py` in the repo root can wipe accumulated frames from the channel between sessions; it uses `BOT_A_TOKEN` (or `BOT_B_TOKEN`) and `BRIDGE_CHAT_ID` from the same `.env`.

## Architecture

There are three optional runtime processes:

- `client`: local FastAPI endpoint used by Claude Code or another Anthropic-compatible client.
- `server`: Telegram relay that receives framed requests and forwards them to Anthropic or to `cc_proxy`.
- `cc_proxy`: optional sidecar that rewrites forwarded requests to look like Claude Code requests, then forwards them to the real `ANTHROPIC_BASE_URL`.

Request flow:

1. Claude Code sends an Anthropic API request to the client URL, for example `http://127.0.0.1:8787/v1/messages`.
2. `client` accepts the HTTP request, normalizes the Anthropic path, keeps only relay-safe headers, and wraps `path`, `headers`, and request body into an internal envelope.
3. `client` sends that envelope to the shared Telegram chat with bot A (`BOT_A_TOKEN`). Small requests use text frames. Large requests may use one compressed Telegram document fallback. Repeated Claude Code payload sections can be replaced with internal cache references before sending.
4. `server` listens to the same Telegram chat with bot B (`BOT_B_TOKEN`). It receives text frames or request documents, reconstructs the internal envelope, restores any cache references from its local SQLite cache DB, and rebuilds the full Anthropic request body.
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

To make `cc_proxy` act as a plain forwarding proxy without Claude Code header
fingerprinting:

```bash
python -m cc_proxy.main --no-claude-code-headers
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
- Protocol-level cache references let repeated Claude Code `tools` and reusable `system`/`messages` content blocks cross the bridge as small 64-hex sha256 refs when the client local SQLite DB already has the canonical JSON bytes.
- Both client and server keep local SQLite cache DBs with a default TTL of 72 hours. The protocol no longer sends `cache_ack`; if the server DB misses a ref, the client can replay the same request envelope in full up to `PROXY_CACHE_CLIENT_HIT_SERVER_MISS_MAX_REPLAYS` times per process.

Known Claude Code cache invalidators:

- Claude Code can prepend a short `system` block like `x-anthropic-billing-header: ...; cch=...;`. The `cch` value has been observed changing between otherwise identical requests. Content-block cache granularity keeps this from invalidating unrelated local bridge cache entries, but Anthropic prompt caching is still prefix-based in `tools -> system -> messages` order, so the changing first `system` block can prevent later upstream prompt-cache breakpoints from matching.
- With Claude Code `2.1.140` and `figma@claude-plugins-official` `2.2.12`, the Figma plugin skill list in the injected `messages[0].content[0]` `<system-reminder>` was observed with nondeterministic ordering. Content-block cache granularity limits the local bridge miss to that injected block, and disabling or uninstalling the Figma plugin removes that source of churn entirely.

Response-side optimizations:

- The first upstream response chunk is flushed immediately so the HTTP client sees bytes early.
- Later SSE bytes are coalesced by time/size thresholds to reduce Telegram `sendMessage` calls.
- Response frames use dynamic encoded-length packing, matching the request-side safety check.
- Server logs include response flush and per-frame Telegram send timing for debugging slow or timed-out requests.

Benchmark notes:

- Telegram channel send-rate measurements are recorded in [docs/benchmarks/telegram-channel-rate-limit.md](docs/benchmarks/telegram-channel-rate-limit.md).

## Development

This project uses [uv](https://docs.astral.sh/uv/) for dependency and virtualenv management. The committed `uv.lock` pins the resolved versions.

Install dependencies (creates `.venv/` and syncs from `uv.lock`):

```bash
uv sync
```

Run the client:

```bash
uv run python -m client.main
```

Run the server:

```bash
uv run python -m server.main
```

The server accepts one optional flag:

- `--use-cc-proxy`: forward upstream Anthropic requests through the local `cc_proxy` sidecar instead of calling `ANTHROPIC_BASE_URL` directly. The server health-checks the sidecar at startup and exits with a clear error if it is not running.

```bash
uv run python -m server.main --use-cc-proxy
```

Run the optional `cc_proxy` sidecar (required when the server is started with `--use-cc-proxy`):

```bash
uv run python -m cc_proxy.main
```

Run checks:

```bash
uv run python -m compileall client server shared cc_proxy
uv run python -m pytest
```
