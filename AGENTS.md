# Repository Guidelines

## Project Structure & Module Organization

This repository implements an Anthropic-compatible API proxy tunneled through a
Telegram bridge.

- `client/`: local FastAPI proxy exposed to Anthropic-compatible clients.
  `client/main.py` starts the HTTP server, `client/tg_client.py` handles
  Telegram frame transport, and `client/routing.py` normalizes Anthropic `/v1`
  paths.
- `server/`: remote relay process. `server/main.py` loads configuration and
  starts `server/relay.py`, which reconstructs bridge requests and forwards them
  to Anthropic or to the optional sidecar.
- `cc_proxy/`: optional FastAPI sidecar used with `server.main --use-cc-proxy`
  to rewrite Claude Code-compatible request headers before forwarding upstream.
- `shared/`: transport-neutral helpers for request IDs, gzip/base64 framing,
  dynamic frame packing, document blobs, cache protocol/DB helpers, and safe logging.
- `scripts/`: maintenance helpers. `scripts/sync_env.py` rewrites a local env
  file from a template while preserving local secret values.
- `docs/tasks/`: task handoff notes for Telegram bridge optimization work.
- `tests/`: pytest suite covering framing, cache behavior, routing, logging,
  `cc_proxy` headers/configuration, and env sync behavior.
- `.env.example`: runtime configuration template. Copy it to `.env` locally and
  keep secrets out of version control.

Run the client and server with matching `BRIDGE_CHAT_ID`; the client uses
`BOT_A_TOKEN`, and the server uses `BOT_B_TOKEN`.

## Build, Test, and Development Commands

- `python -m pip install -e .`: install the package and dependencies from
  `pyproject.toml` in editable mode.
- `python -m client.main`: run the local proxy on `PROXY_HOST` / `PROXY_PORT`.
- `python -m server.main`: run the Telegram relay that forwards requests directly
  to `ANTHROPIC_BASE_URL`.
- `python -m cc_proxy.main`: run the optional Claude Code header sidecar.
- `python -m server.main --use-cc-proxy`: run the relay through the local
  `cc_proxy` sidecar after a startup health check.
- `python scripts/sync_env.py .env .env.example`: refresh local `.env` shape from
  the template without copying secrets into `.env.example`.
- `python -m compileall client server shared cc_proxy`: quick syntax check for
  all runtime modules.
- `python -m pytest`: run the full test suite.
- `python -m pytest tests/test_framing_dynamic_chunks.py`: run a focused test
  file while iterating on framing logic.

## Coding Style & Naming Conventions

Use Python 3.11+ idioms, four-space indentation, and type hints for public or
cross-module functions. Keep module names lowercase with underscores. Prefer
small async helpers over deeply nested logic.

Reuse existing shared modules before adding new abstractions:

- Use `shared/framing.py` for tunnel frame, chunking, and document transport
  changes.
- Use `shared/cache_protocol.py` and `shared/cache_db.py` for active bridge cache
  behavior. `shared/cache_store.py` is legacy in-memory cache coverage.
- Use `shared/logging_utils.py` for redacted headers and compact request-body
  summaries.

Keep diffs small and avoid new dependencies unless explicitly requested.

## Testing Guidelines

Add tests under `tests/` using `test_*.py` file names and `test_*` function
names. Prioritize unit tests for `shared/` helpers, routing, request/response
envelopes, cache behavior, and configuration parsing before integration tests.

Avoid real Telegram or Anthropic calls in unit tests; mock network boundaries and
keep secret-bearing data out of fixtures. For async code, use an async pytest
plugin only if the test suite already depends on one or the change explicitly
requires it.

Before claiming completion for code changes, run at least:

- `python -m compileall client server shared cc_proxy`
- `python -m pytest`

## Commit & Pull Request Guidelines

Use Lore-style commit messages when committing: first line states why the change
was made, the body records the relevant constraints and rationale, and useful
git-native trailers capture verification and risk. Include `Tested:` and
`Not-tested:` trailers whenever they add useful context.

Pull requests should describe the behavioral change, list configuration or
environment impacts, and include test results. For proxy changes, mention
affected endpoints, headers, streaming behavior, cache behavior, framing changes,
and timeout handling.

## Security & Configuration Tips

Never commit `.env`, bot tokens, API keys, chat IDs, request dumps, or response
payloads containing user data. Keep upstream URLs configurable through
`ANTHROPIC_BASE_URL` and sidecar settings configurable through `CC_PROXY_*`.

When logging errors, avoid dumping full request bodies or authorization headers.
Prefer the shared redaction and summarization helpers over ad hoc logging.
