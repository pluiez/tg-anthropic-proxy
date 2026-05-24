# Telegram Channel Send Rate Benchmark

## Purpose

Validate the real `sendMessage` flood-limit behavior for Telegram bots sending
messages to the same bridge channel.

The bridge uses Telegram messages on the critical path in both directions, so
the practical channel send rate directly affects request latency and maximum
throughput.

## Official Guidance

Telegram's Bot API and FAQ should be treated as the source of truth for
protocol behavior:

- Bot API flood limits are reported as `429 Too Many Requests` responses with a
  `retry_after` value.
- Telegram documents a broad bot send budget and advises avoiding sustained
  high-frequency sends to the same chat.
- `python-telegram-bot` treats channels and supergroups as group-like targets
  for `AIORateLimiter` because Bot API `chat_id` values do not let it reliably
  distinguish them in all cases.
- `python-telegram-bot`'s default group/channel limiter constant is
  `20 messages / 60 seconds`.

Telegram does not provide a stable public guarantee that two separate bots
sending to the same channel will always linearly combine their per-bot budgets.
The measurements below are observed behavior for this local bridge channel, not
an official Telegram guarantee.

## Local Benchmark

Date: 2026-05-24

Script: `scripts/telegram_rate_limit_test.py`

Environment:

- Credentials and target channel came from the local `.env`.
- `BOT_A_TOKEN`, `BOT_B_TOKEN`, and `BRIDGE_CHAT_ID` were used.
- Token values and the concrete channel ID are intentionally not recorded.

Method:

- Stage 1 sent messages with a single bot.
- Stage 2 sent messages with two bots to the same channel.
- Each active bot was scheduled at `1 message/second`.
- The script respected Telegram's returned `retry_after` before continuing.
- Messages were sent with `disable_notification=true`.

## Results

### Single Bot

| Metric | Value |
| --- | ---: |
| Bot | `BOT_A_TOKEN` |
| Target rate | `1 msg/s` |
| Attempts | 75 |
| Successes | 72 |
| Failures | 3 |
| `429` responses | 3 |
| `retry_after` values | `39s`, `32s`, `31s` |
| Actual success rate | `0.360 msg/s` |
| Actual success rate | `21.60 msg/min` |
| Max successes in any 60s window | 21 |

Failure points:

| Sequence | Completed offset | `retry_after` |
| ---: | ---: | ---: |
| 21 | `22.07s` | `39s` |
| 42 | `89.77s` | `32s` |
| 63 | `152.03s` | `31s` |

### Dual Bot

| Metric | Bot A | Bot B | Aggregate |
| --- | ---: | ---: | ---: |
| Target rate | `1 msg/s` | `1 msg/s` | `2 msg/s` |
| Attempts | 75 | 75 | 150 |
| Successes | 72 | 72 | 144 |
| Failures | 3 | 3 | 6 |
| `429` responses | 3 | 3 | 6 |
| `retry_after` values | `32s`, `32s`, `34s` | `33s`, `32s`, `32s` | `32-34s` |
| Actual success rate | `0.360 msg/s` | `0.360 msg/s` | `0.720 msg/s` |
| Actual success rate | `21.59 msg/min` | `21.59 msg/min` | `43.18 msg/min` |
| Max successes in any 60s window | 22 | 20 | 42 |

Failure points:

| Bot | Sequence | Completed offset | `retry_after` |
| --- | ---: | ---: | ---: |
| A | 21 | `28.07s` | `32s` |
| B | 21 | `28.54s` | `33s` |
| A | 42 | `88.74s` | `32s` |
| B | 42 | `90.07s` | `32s` |
| A | 63 | `148.26s` | `34s` |
| B | 63 | `151.16s` | `32s` |

## Conclusion

For this bridge channel, observed behavior is closer to a per-bot budget of
about `20 messages/minute` than to a sustained `1 message/second` channel send
rate.

The two-bot test suggests that `BOT_A_TOKEN` and `BOT_B_TOKEN` each received a
separate budget of roughly `20 messages/minute` in the same channel. Their
combined observed throughput was about `40 messages/minute`, but they could not
sustain the scheduled aggregate rate of `2 messages/second`.

Treat this as an operational benchmark for this bridge, not a Telegram API
contract. Repeat the test if the channel type, bot age, account standing, or
Telegram backend behavior changes.

## Implementation Impact

Keep `AIORateLimiter` instances independent per bot/application. Sharing one
limiter between bot A and bot B would be wrong for the observed behavior:

- It would collapse two independently observed per-bot channel budgets into one
  local budget.
- A `RetryAfter` hit by one bot would pause the other bot, because
  `AIORateLimiter` gates requests behind a shared retry-after event.
- The overall bot limit is also token-scoped, so sharing one limiter would merge
  separate bot-token budgets unnecessarily.

The current configured limiter is too optimistic for this channel:

```python
AIORateLimiter(
    overall_max_rate=30,
    overall_time_period=1,
    group_max_rate=3,
    group_time_period=1,
    max_retries=5,
)
```

That allows up to `180 messages/minute` for a group/channel target, which
causes the bridge to hit Telegram's `429` responses and then rely on reactive
`retry_after` backoff.

## Follow-Ups

- Change the per-bot group/channel limiter to `20/60`, or use a more
  conservative `18/60` to avoid edge-window jitter.
- Keep the limiter scoped per bot process.
- Continue optimizing the bridge protocol to reduce Telegram message count,
  especially request EOF and response EOF frame merging.
