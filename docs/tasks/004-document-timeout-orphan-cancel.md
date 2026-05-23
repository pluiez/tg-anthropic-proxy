# Task 004: Document Timeout, Cleanup, And Orphan Cancellation

## 背景

一次慢请求排查中发现了独立可靠性问题：

- client 发送 request document 时，`sendDocument` 在 client 侧超时；
- FastAPI endpoint 返回 `500` 给 Claude Code；
- 但 Telegram 实际随后把完整 document 投递到了 channel；
- server 下载 document，并通过 gzip/raw_size/sha256 校验，说明内容完整；
- server 继续把这个 request 转发给 cc_proxy/upstream；
- Claude Code 同时又发起了新请求，导致两条请求重叠执行，整体耗时被拉长。

这不是 response buffer 本身的问题，而是 request document upload 的 ambiguous timeout 和 orphan request 问题。

## 决策

本任务要提高 request document path 的可靠性，并避免 client 已失败后 server 继续处理孤儿请求。

分三部分：

1. 为 Telegram document upload 配置更合理的 timeout。
2. 修复 client 发送失败时的 `PENDING` 清理。
3. 增加 best-effort cancel/orphan 处理。

## Timeout 配置

当前 `client/tg_client.py` 直接调用：

```python
await _app.bot.send_document(...)
```

未显式传入 timeout。需要新增 `.env.example` 配置，例如：

```dotenv
PROXY_TELEGRAM_DOCUMENT_CONNECT_TIMEOUT=10
PROXY_TELEGRAM_DOCUMENT_WRITE_TIMEOUT=60
PROXY_TELEGRAM_DOCUMENT_READ_TIMEOUT=60
PROXY_TELEGRAM_DOCUMENT_POOL_TIMEOUT=10
```

实现时优先使用 python-telegram-bot `send_document` 支持的 per-request timeout 参数。不要新增依赖。

默认值要考虑当前 document 大小：

- cache 后约 16KB gzip blob；
- 未 cache 首次请求约 40KB+ gzip blob；
- 这些大小不大，但 Telegram API 或网络偶发慢响应会触发默认 read timeout。

## Failure Cleanup

当前 `client/main.py` 中，`PENDING[rid] = q` 在 `_send_envelope()` 之前注册。如果 `_send_envelope()` 抛异常，StreamingResponse generator 不会进入 `finally`，因此 `PENDING` 可能残留。

需要确保：

- `_send_envelope()` 失败时移除 `PENDING[rid]`；
- 返回给 Claude Code 的错误清晰；
- 不留下无人消费的 queue。

具体返回形式可以由实现者按 FastAPI/Claude Code 行为选择，但必须避免 traceback 式 500 污染日志。建议返回 Anthropic/SSE 兼容的错误事件或明确的 HTTP 502。

## Cancel/Orphan 协议

新增 `cancel` frame，client 在以下情况 best-effort 发送：

- `_send_envelope()` 失败；
- client 等待 response 超时；
- client HTTP stream 被取消或断开。

示例：

```json
{"v":1,"rid":"r_x","seq":0,"kind":"cancel","reason":"send_document_timeout"}
```

server 收到 `cancel` 后：

- 将 rid 标记为 cancelled；
- 如果 request document 后到，直接丢弃，不转发 upstream；
- 如果 request 正在处理，尽量取消对应 asyncio task / upstream stream；
- 如果已经完成，忽略。

这是 best-effort 机制，不能保证所有 race 都能阻止，但能减少 orphan request。

## Server 状态管理

建议 server 维护：

- `_CANCELLED`: rid -> expires_at
- `_TASKS`: rid -> asyncio.Task

cancelled rid 需要 TTL 清理，避免内存泄漏。TTL 可以使用 `PROXY_RESPONSE_TIMEOUT` 或新增配置，默认几分钟即可。

处理 document 时：

1. parse caption 拿 rid；
2. 如果 rid 已 cancelled，记录日志并 drop；
3. 否则下载、校验、处理。

处理 text frame 时：

- 如果 rid 已 cancelled，忽略后续 `req` / `req_end` / `req eof=true`。

处理 `_process()` 时：

- 开始前检查 cancelled；
- upstream streaming loop 中可以周期性检查 cancelled；
- 被 cancel 后不要继续发送 response frame。

## 注意事项

- `sendDocument` 超时不代表 Telegram 没收到完整 document。它只代表 client 没在 timeout 内拿到 Bot API 响应。
- 如果 server 已经收到并校验通过 document，说明 request 内容完整。
- cancel frame 自身也是一条 Telegram 消息，只应在失败/超时/断开时发送，不应出现在正常成功路径。
- cancel 和 document 可能乱序。server 必须能处理“cancel 先到、document 后到”的情况。
- 如果 document 已经进入 upstream，cancel 只能 best-effort 中断。
- 日志中不要泄漏 bot token、authorization、request body。

## 验收标准

- `send_document` timeout 可通过 `.env` 配置。
- `_send_envelope()` 抛异常时 `PENDING` 会清理。
- 不再出现 request 发送失败后无人消费 queue 的情况。
- client response timeout 或 disconnect 时会 best-effort 发 cancel。
- server 收到 cancel 后能丢弃后到的 document。
- server 对正在处理的 task 尽量取消，并停止继续发送 response。
- tests 覆盖：
  - send envelope failure cleans pending；
  - cancel before document drops document；
  - cancel during processing stops response send；
  - normal document path unaffected。

## 非目标

- 本任务不改变 cache 协议。
- 本任务不减少正常 response 的 Telegram 消息数。
- 本任务不实现 response document fallback。

