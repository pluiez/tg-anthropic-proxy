# Repository Guidelines

## Project Structure & Module Organization

This repository implements an Anthropic API proxy tunneled through a Telegram bridge.

- `client/`: local FastAPI proxy exposed to Anthropic-compatible clients. `client/main.py` starts the HTTP server, and `client/tg_client.py` handles Telegram frame transport.
- `server/`: remote relay process. `server/main.py` loads environment variables and starts `server/relay.py`, which forwards framed requests to the Anthropic upstream.
- `shared/`: transport-neutral helpers, currently request IDs, gzip/base64 framing, and chunking in `shared/framing.py`.
- `.env.example`: required runtime configuration. Copy it to `.env` locally and fill secrets there.

There is no committed `tests/` directory yet. Add tests under `tests/` using module-oriented names such as `tests/test_framing.py`.

## Build, Test, and Development Commands

- `python -m pip install -e .`: install the package and dependencies from `pyproject.toml` in editable mode.
- `python -m client.main`: run the local proxy on `PROXY_HOST` / `PROXY_PORT` from `.env`.
- `python -m server.main`: run the Telegram relay that forwards requests to `ANTHROPIC_BASE_URL`.
- `python -m compileall client server shared`: quick syntax check for all Python modules.
- `python -m pytest`: run tests once a `tests/` suite is added.

Run the client and server with matching `BRIDGE_CHAT_ID`; the client uses `BOT_A_TOKEN`, and the server uses `BOT_B_TOKEN`.

## Coding Style & Naming Conventions

Use Python 3.11+ idioms, four-space indentation, and type hints for public or cross-module functions. Keep module names lowercase with underscores. Prefer small async helpers over deeply nested logic, and reuse `shared/framing.py` for any tunnel frame changes instead of duplicating framing code.

## Testing Guidelines

Prioritize unit tests for `shared/` helpers and request/response envelope behavior before adding integration tests. Name test files `test_*.py` and test functions `test_*`. For async code, use an async pytest plugin if introduced later; avoid real Telegram or Anthropic calls in unit tests.

## Commit & Pull Request Guidelines

This checkout has no local Git history to infer conventions from. Use concise, imperative commit subjects that explain intent. Include verification details in the body, especially `Tested:` and `Not-tested:` trailers when relevant.

Pull requests should describe the behavioral change, list configuration or environment impacts, and include test results. For proxy changes, mention affected endpoints, headers, streaming behavior, and timeout handling.

## Security & Configuration Tips

Never commit `.env`, bot tokens, API keys, or chat IDs. Keep upstream URLs configurable through `ANTHROPIC_BASE_URL`. When logging errors, avoid dumping full request bodies or authorization headers.
