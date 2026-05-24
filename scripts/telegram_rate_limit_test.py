#!/usr/bin/env python3
"""Measure Telegram Bot API sendMessage rate behavior in the bridge channel."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from dotenv import dotenv_values


DEFAULT_ENV_PATH = Path(".env")
DEFAULT_OUTPUT_DIR = Path(".cache/telegram-rate-test")


@dataclass
class SendRecord:
    bot: str
    seq: int
    scheduled_offset_s: float
    started_offset_s: float
    completed_offset_s: float
    latency_s: float
    status_code: int | None
    ok: bool
    message_id: int | None
    error_code: int | None
    retry_after: int | None
    description: str | None
    exception: str | None


def require_env(config: dict[str, str | None], key: str, env_path: Path) -> str:
    value = config.get(key)
    if value is None or not str(value).strip():
        raise SystemExit(f"missing required env var in {env_path}: {key}")
    return str(value).strip()


def sliding_window_max(values: list[float], window_s: float) -> int:
    if not values:
        return 0
    start = 0
    best = 0
    for end, value in enumerate(values):
        while value - values[start] > window_s:
            start += 1
        best = max(best, end - start + 1)
    return best


def summarize_records(records: list[SendRecord], elapsed_s: float) -> dict[str, Any]:
    by_bot: dict[str, list[SendRecord]] = defaultdict(list)
    for record in records:
        by_bot[record.bot].append(record)

    def summarize_subset(subset: list[SendRecord]) -> dict[str, Any]:
        attempts = len(subset)
        successes = [record for record in subset if record.ok]
        failures = [record for record in subset if not record.ok]
        success_times = sorted(record.completed_offset_s for record in successes)
        retry_afters = [record.retry_after for record in subset if record.retry_after is not None]
        latencies = [record.latency_s for record in subset]
        span = max(elapsed_s, 0.001)
        return {
            "attempts": attempts,
            "successes": len(successes),
            "failures": len(failures),
            "success_messages_per_second": len(successes) / span,
            "success_messages_per_minute": len(successes) / span * 60,
            "max_successes_in_any_60s_window": sliding_window_max(success_times, 60.0),
            "status_codes": {
                str(key): value for key, value in Counter(record.status_code for record in subset).items()
            },
            "error_codes": {
                str(key): value
                for key, value in Counter(record.error_code for record in subset if record.error_code is not None).items()
            },
            "retry_after_values": retry_afters,
            "latency_s": {
                "min": min(latencies) if latencies else None,
                "max": max(latencies) if latencies else None,
                "avg": sum(latencies) / len(latencies) if latencies else None,
            },
        }

    return {
        "elapsed_s": elapsed_s,
        "aggregate": summarize_subset(records),
        "by_bot": {bot: summarize_subset(subset) for bot, subset in sorted(by_bot.items())},
    }


async def get_bot_identity(client: httpx.AsyncClient, token: str) -> dict[str, Any]:
    response = await client.get(f"https://api.telegram.org/bot{token}/getMe")
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(f"getMe failed: status={response.status_code} payload={payload}")
    result = payload.get("result", {})
    return {
        "id": result.get("id"),
        "username": result.get("username"),
        "first_name": result.get("first_name"),
    }


async def send_message(
    client: httpx.AsyncClient,
    *,
    token: str,
    chat_id: str,
    bot_label: str,
    run_id: str,
    seq: int,
    scheduled_at: float,
    test_started_at: float,
) -> SendRecord:
    started_at = time.monotonic()
    status_code: int | None = None
    payload: dict[str, Any] = {}
    exception: str | None = None
    try:
        response = await client.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": f"tg-rate-test run={run_id} bot={bot_label} seq={seq:04d}",
                "disable_notification": True,
            },
        )
        status_code = response.status_code
        try:
            payload = response.json()
        except json.JSONDecodeError:
            payload = {"ok": False, "description": response.text[:500]}
    except Exception as exc:  # noqa: BLE001 - diagnostics script should report all transport errors.
        exception = f"{type(exc).__name__}: {exc}"
    completed_at = time.monotonic()

    parameters = payload.get("parameters") if isinstance(payload.get("parameters"), dict) else {}
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    return SendRecord(
        bot=bot_label,
        seq=seq,
        scheduled_offset_s=scheduled_at - test_started_at,
        started_offset_s=started_at - test_started_at,
        completed_offset_s=completed_at - test_started_at,
        latency_s=completed_at - started_at,
        status_code=status_code,
        ok=bool(payload.get("ok")),
        message_id=result.get("message_id"),
        error_code=payload.get("error_code"),
        retry_after=parameters.get("retry_after"),
        description=payload.get("description"),
        exception=exception,
    )


async def run_sender(
    client: httpx.AsyncClient,
    *,
    token: str,
    chat_id: str,
    bot_label: str,
    run_id: str,
    count: int,
    interval_s: float,
    test_started_at: float,
    respect_retry_after: bool,
    records: list[SendRecord],
) -> None:
    next_send_at = test_started_at
    for seq in range(1, count + 1):
        now = time.monotonic()
        if next_send_at > now:
            await asyncio.sleep(next_send_at - now)

        record = await send_message(
            client,
            token=token,
            chat_id=chat_id,
            bot_label=bot_label,
            run_id=run_id,
            seq=seq,
            scheduled_at=next_send_at,
            test_started_at=test_started_at,
        )
        records.append(record)

        # Do not catch up after a slow request. Catch-up bursts would exceed
        # the requested per-bot rate and make channel limits harder to isolate.
        next_send_at = max(
            next_send_at + interval_s,
            test_started_at + record.started_offset_s + interval_s,
        )
        if respect_retry_after and record.retry_after:
            next_send_at = max(next_send_at, time.monotonic() + float(record.retry_after))


async def run_test(args: argparse.Namespace) -> dict[str, Any]:
    env_path = Path(args.env)
    config = dotenv_values(env_path)
    chat_id = require_env(config, "BRIDGE_CHAT_ID", env_path)
    bot_a_token = require_env(config, "BOT_A_TOKEN", env_path)

    bots = [("A", bot_a_token)]
    if args.stage == "dual":
        bot_b_token = require_env(config, "BOT_B_TOKEN", env_path)
        bots.append(("B", bot_b_token))

    count = int(math.ceil(args.duration_s * args.rate_per_bot))
    interval_s = 1.0 / args.rate_per_bot
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    timeout = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)
    records: list[SendRecord] = []

    async with httpx.AsyncClient(timeout=timeout) as client:
        identities = {
            label: await get_bot_identity(client, token)
            for label, token in bots
        }
        test_started_at = time.monotonic()
        await asyncio.gather(
            *[
                run_sender(
                    client,
                    token=token,
                    chat_id=chat_id,
                    bot_label=label,
                    run_id=run_id,
                    count=count,
                    interval_s=interval_s,
                    test_started_at=test_started_at,
                    respect_retry_after=args.respect_retry_after,
                    records=records,
                )
                for label, token in bots
            ]
        )
        completed_at = time.monotonic()

    records.sort(key=lambda record: (record.completed_offset_s, record.bot, record.seq))
    elapsed_s = completed_at - test_started_at
    return {
        "run_id": run_id,
        "stage": args.stage,
        "env_path": str(env_path),
        "chat_id_present": bool(chat_id),
        "bot_identities": identities,
        "configured": {
            "duration_s": args.duration_s,
            "rate_per_bot": args.rate_per_bot,
            "count_per_bot": count,
            "respect_retry_after": args.respect_retry_after,
        },
        "summary": summarize_records(records, elapsed_s),
        "records": [asdict(record) for record in records],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Measure Telegram channel sendMessage rate behavior with bridge bot credentials."
    )
    parser.add_argument("--stage", choices=["single", "dual"], default="single")
    parser.add_argument("--env", default=str(DEFAULT_ENV_PATH), help="env file containing BOT_A_TOKEN/BOT_B_TOKEN/BRIDGE_CHAT_ID")
    parser.add_argument("--duration-s", type=float, default=75.0, help="duration to schedule sends for each bot")
    parser.add_argument("--rate-per-bot", type=float, default=1.0, help="messages per second scheduled for each bot")
    parser.add_argument(
        "--no-respect-retry-after",
        action="store_false",
        dest="respect_retry_after",
        help="continue the fixed schedule even after Telegram returns retry_after",
    )
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.set_defaults(respect_retry_after=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.rate_per_bot <= 0:
        raise SystemExit("--rate-per-bot must be positive")
    if args.duration_s <= 0:
        raise SystemExit("--duration-s must be positive")

    result = asyncio.run(run_test(args))
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{result['run_id']}-{result['stage']}.json"
    output_path.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")

    printable = {
        "run_id": result["run_id"],
        "stage": result["stage"],
        "configured": result["configured"],
        "summary": result["summary"],
        "output_path": str(output_path),
    }
    print(json.dumps(printable, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
