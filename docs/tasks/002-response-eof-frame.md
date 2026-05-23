# Task 002: Response eof Frame Merge

## 背景

当前 response path 中，server 通过 `bot_b` 把 upstream SSE response 发回 Telegram channel：

1. upstream SSE bytes 进入 response buffer；
2. server 按 `PROXY_RESPONSE_FLUSH_INTERVAL` 或 `PROXY_RESPONSE_FLUSH_BYTES` flush；
3. flush 出来的 raw bytes 被 gzip/base64，并按 `PROXY_TELEGRAM_RESPONSE_FRAME_MAX_CHARS` 切成 `resp_chunk`；
4. response 完成后，server 额外发送一条 `resp_end` frame。

真实日志显示，在当前优化后，很多 Claude Code response 是：

- 1 条 `resp_chunk` for first chunk；
- 1 条 `resp_chunk` for eof buffer；
- 1 条 `resp_end`。

因此每个小 response 仍然有一条纯结束信号消息。

## 决策

给最后一个 `resp_chunk` 增加 `eof=true`，让它同时承担“最后一段 payload”和“response 结束”的语义，从而去掉单独的 `resp_end`。

示例：

```json
{"v":1,"rid":"r_x","seq":1,"kind":"resp_chunk","data":"...","eof":true}
```

client 收到 `resp_chunk` 时：

- 先把 payload 写入 SSE stream；
- 如果 `eof=true`，再结束 stream。

## 重要边界

这项优化不保证每个 response 都稳定少一条 Telegram 消息。

如果 EOF 时 buffer 里还有数据，`eof=true` 可以挂在最后一个 payload frame 上，少发一条消息。

如果 EOF 时 buffer 已经被 interval/size flush 空了，server 仍然需要发送一个空 payload 的 `resp_chunk eof=true`，否则 client 无法知道 response 已结束。这种情况下消息数和旧的 `resp_end` 一样。

## 与 cache_ack 的关系

如果 Task 001 已完成并删除了 `cache_ack`，本任务无需处理 cache ack piggyback。

如果 Task 001 暂未完成，理论上可以把 `cache_ack_keys` 顺带挂到最后一个 `resp_chunk eof=true` 上，但不建议先做这个临时方案。因为 DB cache 会让 `cache_ack` 失去必要性，先做 piggyback 会增加短期协议复杂度。

## 需要改动的区域

重点文件：

- `server/relay.py`
- `client/main.py`
- `shared/framing.py`
- tests under `tests/`

当前 `chunk_bytes_for_frame_payloads(..., extra=...)` 会把 `extra` 加到每一个 split frame 上。`eof=true` 只能出现在最后一个 response frame 上，不能出现在同一次 flush 的所有 frame 上。

实现时可以选择：

- 增加支持 `last_extra` 的 helper；或
- 在 `server/relay.py` 中手动给最后一个 frame 加 `eof=true`。

无论选择哪种方式，都要确保 frame length 仍然不超过 `PROXY_TELEGRAM_RESPONSE_FRAME_MAX_CHARS`。

## 兼容性

client 应同时兼容：

- 旧协议：`resp_chunk` + `resp_end`
- 新协议：最后一个 `resp_chunk eof=true`

server 新逻辑可以只发送新协议，但 tests 应覆盖 client 对旧协议的兼容。

## 注意事项

- `eof=true` frame 的 payload 可能为空。
- 如果 EOF payload 被动态切成多条 text frame，只有最后一条应该带 `eof=true`。
- client 必须先 yield payload，再处理 eof，否则最后一段 SSE 数据会丢失。
- 不要改变 upstream SSE bytes 内容；只改变 Telegram frame envelope。
- `resp_error` 语义不变。

## 验收标准

- 小 response 在 EOF buffer 非空时不再发送单独 `resp_end`。
- client 能正确结束 stream，并且最后一段 SSE payload 不丢失。
- EOF buffer 为空时，server 发送空 payload `resp_chunk eof=true` 或等价结束 frame，client 能结束 stream。
- client 仍能处理旧的 `resp_end`。
- tests 覆盖：
  - payload + `eof=true`；
  - empty payload + `eof=true`；
  - split payload 只有最后一帧带 `eof=true`；
  - old `resp_end` compatibility。

## 非目标

- 本任务不调整 response buffer 参数。
- 本任务不实现 response document fallback。
- 本任务不处理 request path 的 `req_end`；那是 Task 003。

