# Telegram Bridge Optimization Handoff

本文档目录用于把当前排查结论拆成交接给 Claude Code 的独立任务。当前基线是用户在 2026-05-24 做过 git checkpoint 后的代码库；最近一次可见提交为 `e228a4c docs(env): document proxy transport tuning`。

## 当前共识

项目是一个 Anthropic API over Telegram bridge：

1. Claude Code 或 curl 请求本地 `client`。
2. `client` 用 `bot_a` 把 Anthropic request 通过 Telegram channel 发给 `server`。
3. `server` 用 `bot_b` 监听 Telegram channel，重建 Anthropic request。
4. `server` 直接请求 `.env` 中的 `ANTHROPIC_BASE_URL`，或在 `--use-cc-proxy` 时请求本地 `cc_proxy`。
5. `cc_proxy` 转发到真实 `ANTHROPIC_BASE_URL`，并统一做 Claude Code fingerprint/header 改写。
6. upstream SSE response 再由 `server` 用 `bot_b` 发回 Telegram channel，`client` 收到后作为 SSE stream 返回给 Claude Code。

当前 request path 已经有：

- protocol-level cache/ref；
- Telegram text frame 动态打包；
- 大 request 的 Telegram document fallback。

当前 response path 已经有：

- raw SSE bytes buffer；
- 按 `PROXY_RESPONSE_FLUSH_INTERVAL` 或 `PROXY_RESPONSE_FLUSH_BYTES` flush；
- flush 后按 `PROXY_TELEGRAM_RESPONSE_FRAME_MAX_CHARS` 动态切 Telegram text frame。

重要参数语义：

- `PROXY_RESPONSE_FLUSH_INTERVAL`：response buffer 最长等待时间。
- `PROXY_RESPONSE_FLUSH_BYTES`：未压缩 raw SSE bytes 累积到多少就 flush。
- `PROXY_TELEGRAM_RESPONSE_FRAME_MAX_CHARS`：单条 Telegram text frame 的最大字符数；gzip/base64/JSON frame 后的长度只在生成 frame 时受它约束。

## 设计目标

核心目标不是单纯调大 buffer，而是减少 `bot_a` 和 `bot_b` 发到同一个 Telegram channel 的消息总量，同时保持流式响应语义和可恢复性。

不能假设 Telegram 对同一个 channel 里多个 bot 的发送速率限制完全独立。即使实际按 bot token 独立限流，减少每个 bot 的消息数仍然有价值。

## 建议实施顺序

1. [DB cache and remove cache_ack](./001-db-cache-remove-cache-ack.md)
2. [Response eof frame merge](./002-response-eof-frame.md)
3. [Request text eof frame merge](./003-request-eof-frame.md)
4. [Document timeout, cleanup, and orphan cancellation](./004-document-timeout-orphan-cancel.md)
5. [Future: hybrid response document fallback](./005-future-response-document-hybrid.md)

顺序理由：

- DB cache 会改变协议主体，并让 `cache_ack` 失去必要性，应先做。
- `resp_chunk eof=true` 是 response path 的低风险消息数优化，适合在移除 `cache_ack` 后做。
- request text `eof=true` 只影响 text-frame request；Claude Code 大请求当前主要走 document fallback，收益较小。
- `sendDocument` timeout/orphan 是独立可靠性问题，和 response path 优化分开处理更容易验证。
- response document fallback 是未来大响应场景的 hybrid 优化，不是当前第一优先级。

