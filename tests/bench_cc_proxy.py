"""Benchmark cc_proxy overhead vs direct passthrough.

Spins up a local mock upstream that simulates network latency and an
optional gzip-compressible response body, then runs benchmarks against:

  1. DIRECT (no proxy) — baseline
  2. cc_proxy default — current behaviour
  3. cc_proxy +nojson — skip summarize_json_body() in the request log path
  4. cc_proxy +keepalive — httpx limits with keepalive_expiry=300s, 32 keep-alive conns
  5. cc_proxy +gzip — let upstream gzip the response (drop forced identity)
  6. cc_proxy +quiet — root logger at WARNING (silences per-request INFO writes)
  7. cc_proxy +h1 — disable HTTP/2 on the upstream client
  8. cc_proxy +ALL — every optimisation above stacked

Each variant is launched as its own subprocess so monkey-patches do not leak.

Usage:
    python tests/bench_cc_proxy.py              # quick warm-only run (~1 min)
    python tests/bench_cc_proxy.py --cold       # also measure cold (keepalive_expiry) effect
    python tests/bench_cc_proxy.py --body-size 200000  # bigger request body
    python tests/bench_cc_proxy.py --resp-size 50000   # bigger response body
    python tests/bench_cc_proxy.py --iters 50          # more samples

The mock upstream is fully local so connect overhead is ~0; the cold test
inserts a >5s pause between requests, which on localhost mainly measures the
keepalive_expiry knob, not real TLS handshake cost. For a WAN-style measure
re-run the bench against a real (cheap) endpoint with --upstream.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Inline scripts that get written to tempfiles and run as subprocesses.
# ---------------------------------------------------------------------------

MOCK_SCRIPT = '''
import asyncio
import gzip
import json
import os
import sys

from fastapi import FastAPI, Request
from fastapi.responses import Response
import uvicorn

LATENCY_MS = int(os.environ.get("MOCK_LATENCY_MS", "50"))
RESP_BYTES = int(os.environ.get("MOCK_RESP_BYTES", "8192"))
LOG_LEVEL = os.environ.get("MOCK_LOG_LEVEL", "warning")

# Pre-build a body that compresses well (repeated short string) and a body
# that does not (random-looking bytes). The bench uses the compressible one
# so the +gzip variant has something to win.
_COMPRESSIBLE = ("the quick brown fox jumps over the lazy dog. " * 4096)
_BODY = json.dumps({"input_tokens": 1234, "padding": _COMPRESSIBLE[:RESP_BYTES]}).encode()
_GZIPPED = gzip.compress(_BODY)

app = FastAPI()


async def _handle(req: Request) -> Response:
    _ = await req.body()
    if LATENCY_MS > 0:
        await asyncio.sleep(LATENCY_MS / 1000.0)
    accept = req.headers.get("accept-encoding", "")
    if "gzip" in accept:
        return Response(
            content=_GZIPPED,
            media_type="application/json",
            headers={"content-encoding": "gzip", "content-length": str(len(_GZIPPED))},
        )
    return Response(
        content=_BODY,
        media_type="application/json",
        headers={"content-length": str(len(_BODY))},
    )


@app.post("/v1/messages/count_tokens")
async def count_tokens(req: Request) -> Response:
    return await _handle(req)


@app.post("/v1/messages")
async def messages(req: Request) -> Response:
    return await _handle(req)


if __name__ == "__main__":
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=int(os.environ["MOCK_PORT"]),
        log_level=LOG_LEVEL,
        access_log=False,
    )
'''


VARIANT_RUNNER = '''
import logging
import os
import sys

PATCHES = set(os.environ.get("CCP_PATCHES", "").split(","))
PATCHES.discard("")

# --- patches that must land BEFORE cc_proxy.main creates its AsyncClient ---
import httpx

_orig_init = httpx.AsyncClient.__init__


def _patched_init(self, *args, **kwargs):
    if "keepalive" in PATCHES:
        kwargs["limits"] = httpx.Limits(
            max_connections=100,
            max_keepalive_connections=32,
            keepalive_expiry=300.0,
        )
    if "h1" in PATCHES:
        kwargs["http2"] = False
    return _orig_init(self, *args, **kwargs)


httpx.AsyncClient.__init__ = _patched_init

# Quiet uvicorn / fastapi before they configure their own loggers.
if "quiet" in PATCHES:
    os.environ["LOG_LEVEL"] = "WARNING"

# --- now import cc_proxy.main; lifespan will pick up patched httpx ---
import cc_proxy.main as ccp_main
import cc_proxy.headers as ccp_headers

if "nojson" in PATCHES:
    def _fast(body):
        return {"bytes": len(body)}
    ccp_main.summarize_json_body = _fast

if "gzip" in PATCHES:
    _orig_build = ccp_headers.build_claude_code_headers

    def _patched_build(incoming, **kw):
        out = _orig_build(incoming, **kw)
        out["accept-encoding"] = "gzip"
        return out

    ccp_main.build_claude_code_headers = _patched_build

if "quiet" in PATCHES:
    logging.getLogger().setLevel(logging.WARNING)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)

import uvicorn

uvicorn.run(
    ccp_main.app,
    host="127.0.0.1",
    port=int(os.environ["CCP_PORT"]),
    log_level="warning",
    access_log=False,
)
'''


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _free_port() -> int:
    s = socket.socket()
    s.bind(("", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_for_port(host: str, port: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            sock = socket.create_connection((host, port), timeout=0.5)
            sock.close()
            return True
        except OSError:
            time.sleep(0.05)
    return False


def _percentiles(samples: list[float]) -> tuple[float, float, float, float]:
    if not samples:
        return (0.0, 0.0, 0.0, 0.0)
    s = sorted(samples)
    n = len(s)

    def p(q: float) -> float:
        idx = max(0, min(n - 1, int(round(n * q / 100.0)) - 1))
        return s[idx]

    return (sum(s) / n, p(50), p(95), p(99))


def _build_body(target_bytes: int) -> bytes:
    """Produce a JSON body around target_bytes that summarize_json_body() will
    walk fully (deep messages + tools).
    """
    msg_text = "hello world " * 80  # ~960 bytes per message
    messages = [{"role": "user", "content": msg_text} for _ in range(5)]
    tools = [
        {
            "name": f"tool_{i}",
            "description": "x" * 300,
            "input_schema": {
                "type": "object",
                "properties": {f"p{j}": {"type": "string"} for j in range(8)},
            },
        }
        for i in range(20)
    ]
    body = {
        "model": "claude-opus-4-7",
        "messages": messages,
        "tools": tools,
        "system": "You are helpful. " * 30,
    }
    raw = json.dumps(body).encode()
    # Pad messages until we hit the target size.
    while len(raw) < target_bytes:
        body["messages"].append({"role": "user", "content": msg_text * 4})
        raw = json.dumps(body).encode()
    return raw


async def _measure(
    url: str,
    body: bytes,
    headers: dict[str, str],
    *,
    n: int,
    cold_pause: float,
) -> list[float]:
    latencies: list[float] = []
    async with httpx.AsyncClient(timeout=30.0, http2=False) as client:
        # Warmup: 3 untimed requests to settle TLS / DNS / connect / event loop.
        for _ in range(3):
            try:
                await client.post(url, content=body, headers=headers)
            except Exception:
                pass
        await asyncio.sleep(0.2)

        for _ in range(n):
            if cold_pause > 0:
                await asyncio.sleep(cold_pause)
            t0 = time.perf_counter()
            try:
                r = await client.post(url, content=body, headers=headers)
                _ = r.content
            except Exception as exc:
                print(f"   ! request error: {exc}", file=sys.stderr)
                continue
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            if r.status_code >= 400:
                print(
                    f"   ! status={r.status_code} body={r.text[:200]!r}",
                    file=sys.stderr,
                )
            latencies.append(elapsed_ms)
    return latencies


def _spawn_mock(tmp: Path, latency_ms: int, resp_bytes: int) -> tuple[subprocess.Popen, int]:
    mock_path = tmp / "mock_upstream.py"
    mock_path.write_text(MOCK_SCRIPT)
    port = _free_port()
    env = os.environ.copy()
    env["MOCK_PORT"] = str(port)
    env["MOCK_LATENCY_MS"] = str(latency_ms)
    env["MOCK_RESP_BYTES"] = str(resp_bytes)
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.Popen(
        [sys.executable, "-u", str(mock_path)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if not _wait_for_port("127.0.0.1", port, timeout=15):
        proc.kill()
        raise RuntimeError("mock upstream failed to start")
    return proc, port


def _spawn_proxy(
    tmp: Path, patches: list[str], mock_port: int
) -> tuple[subprocess.Popen, int]:
    runner_path = tmp / "variant_runner.py"
    runner_path.write_text(VARIANT_RUNNER)
    port = _free_port()
    env = os.environ.copy()
    env["CCP_PORT"] = str(port)
    env["CCP_PATCHES"] = ",".join(patches)
    env["ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{mock_port}"
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    # Make sure dotenv in cc_proxy.main can't override our ANTHROPIC_BASE_URL.
    # python-dotenv defaults to override=False so this is already safe, but be
    # explicit about LOG_LEVEL so the default INFO behaviour is reproduced.
    env.setdefault("LOG_LEVEL", "INFO")
    proc = subprocess.Popen(
        [sys.executable, "-u", str(runner_path)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if not _wait_for_port("127.0.0.1", port, timeout=15):
        proc.kill()
        raise RuntimeError(f"cc_proxy variant {patches!r} failed to start")
    return proc, port


def _stop(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3)


# ---------------------------------------------------------------------------
# Main.
# ---------------------------------------------------------------------------


VARIANTS: list[tuple[str, list[str]]] = [
    ("cc_proxy default",      []),
    ("cc_proxy +nojson",      ["nojson"]),
    ("cc_proxy +keepalive",   ["keepalive"]),
    ("cc_proxy +gzip",        ["gzip"]),
    ("cc_proxy +quiet",       ["quiet"]),
    ("cc_proxy +h1",          ["h1"]),
    ("cc_proxy +ALL",         ["nojson", "keepalive", "gzip", "quiet"]),
]


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iters", type=int, default=40,
                        help="warm iterations per variant (default 40)")
    parser.add_argument("--cold-iters", type=int, default=8,
                        help="cold iterations per variant when --cold is set")
    parser.add_argument("--cold", action="store_true",
                        help="also run cold (>5s gap) iterations")
    parser.add_argument("--cold-pause", type=float, default=6.0,
                        help="seconds to pause between cold iterations")
    parser.add_argument("--body-size", type=int, default=80_000,
                        help="approx request body size in bytes (default 80 KB)")
    parser.add_argument("--resp-size", type=int, default=8_192,
                        help="approx response body size in bytes (default 8 KB)")
    parser.add_argument("--latency-ms", type=int, default=50,
                        help="mock upstream simulated latency per request (default 50ms)")
    parser.add_argument("--path", default="/v1/messages/count_tokens",
                        help="endpoint to hit on the proxy / upstream")
    args = parser.parse_args()

    body = _build_body(args.body_size)
    headers = {
        "content-type": "application/json",
        "authorization": "Bearer test-token-1234567890abcdef-padding",
        "x-claude-code-session-id": "bench-session",
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "claude-code-20250219,context-management-2025-06-27",
        "user-agent": "claude-cli/2.1.140 (external, cli)",
        "accept": "application/json",
        "x-stainless-arch": "x64",
        "x-stainless-lang": "js",
    }

    tmp = Path(tempfile.mkdtemp(prefix="ccpbench_"))
    print(f"# tmpdir          = {tmp}")
    print(f"# request body    = {len(body):,} bytes")
    print(f"# response body   = ~{args.resp_size:,} bytes (compressible)")
    print(f"# mock latency    = {args.latency_ms} ms per request")
    print(f"# iters (warm)    = {args.iters}")
    if args.cold:
        print(f"# iters (cold)    = {args.cold_iters}  (pause {args.cold_pause}s between)")
    print()

    mock_proc, mock_port = _spawn_mock(tmp, args.latency_ms, args.resp_size)
    print(f"# mock upstream   = http://127.0.0.1:{mock_port}")

    results: list[tuple[str, tuple, tuple]] = []  # (label, warm stats, cold stats)
    try:
        # ---- baseline: direct to mock ----
        direct_url = f"http://127.0.0.1:{mock_port}{args.path}"
        print(f"\n=== DIRECT (no proxy) ===")
        warm = await _measure(direct_url, body, headers, n=args.iters, cold_pause=0)
        cold = []
        if args.cold:
            cold = await _measure(direct_url, body, headers,
                                  n=args.cold_iters, cold_pause=args.cold_pause)
        warm_stats = _percentiles(warm)
        cold_stats = _percentiles(cold) if cold else (0, 0, 0, 0)
        print(f"  warm: mean={warm_stats[0]:6.1f}  p50={warm_stats[1]:6.1f}  "
              f"p95={warm_stats[2]:6.1f}  p99={warm_stats[3]:6.1f}  ms (n={len(warm)})")
        if cold:
            print(f"  cold: mean={cold_stats[0]:6.1f}  p50={cold_stats[1]:6.1f}  "
                  f"p95={cold_stats[2]:6.1f}  p99={cold_stats[3]:6.1f}  ms (n={len(cold)})")
        results.append(("DIRECT (no proxy)", warm_stats, cold_stats))

        # ---- cc_proxy variants ----
        for label, patches in VARIANTS:
            print(f"\n=== {label} ===  (patches={patches or 'none'})")
            try:
                proxy_proc, proxy_port = _spawn_proxy(tmp, patches, mock_port)
            except RuntimeError as exc:
                print(f"  ! {exc}", file=sys.stderr)
                continue
            proxy_url = f"http://127.0.0.1:{proxy_port}{args.path}"
            try:
                warm = await _measure(proxy_url, body, headers,
                                      n=args.iters, cold_pause=0)
                cold = []
                if args.cold:
                    cold = await _measure(proxy_url, body, headers,
                                          n=args.cold_iters,
                                          cold_pause=args.cold_pause)
            finally:
                _stop(proxy_proc)
            warm_stats = _percentiles(warm)
            cold_stats = _percentiles(cold) if cold else (0, 0, 0, 0)
            print(f"  warm: mean={warm_stats[0]:6.1f}  p50={warm_stats[1]:6.1f}  "
                  f"p95={warm_stats[2]:6.1f}  p99={warm_stats[3]:6.1f}  ms (n={len(warm)})")
            if cold:
                print(f"  cold: mean={cold_stats[0]:6.1f}  p50={cold_stats[1]:6.1f}  "
                      f"p95={cold_stats[2]:6.1f}  p99={cold_stats[3]:6.1f}  ms (n={len(cold)})")
            results.append((label, warm_stats, cold_stats))

    finally:
        _stop(mock_proc)

    # ---- summary table ----
    print("\n" + "=" * 92)
    print(f"{'Variant':<26} {'warm mean':>10} {'warm p50':>10} {'warm p99':>10}"
          f" {'Δ p50 vs direct':>18}")
    print("-" * 92)
    direct_warm_p50 = results[0][1][1]
    for label, (w_mean, w50, w95, w99), _cold in results:
        delta = w50 - direct_warm_p50
        sign = "+" if delta >= 0 else ""
        print(f"{label:<26} {w_mean:>10.1f} {w50:>10.1f} {w99:>10.1f}"
              f" {sign}{delta:>14.1f} ms")
    if args.cold:
        print()
        print(f"{'Variant':<26} {'cold mean':>10} {'cold p50':>10} {'cold p99':>10}"
              f" {'Δ p50 vs direct':>18}")
        print("-" * 92)
        direct_cold_p50 = results[0][2][1]
        for label, _warm, (c_mean, c50, c95, c99) in results:
            delta = c50 - direct_cold_p50
            sign = "+" if delta >= 0 else ""
            print(f"{label:<26} {c_mean:>10.1f} {c50:>10.1f} {c99:>10.1f}"
                  f" {sign}{delta:>14.1f} ms")
    print("=" * 92)
    print()
    print("Reading the table:")
    print("  - 'cc_proxy default' Δ vs DIRECT = total cc_proxy overhead.")
    print("  - Each '+xxx' shows what one targeted change recovers.")
    print("  - '+ALL' is the stacked best case; gap to DIRECT is irreducible overhead.")


if __name__ == "__main__":
    asyncio.run(main())
