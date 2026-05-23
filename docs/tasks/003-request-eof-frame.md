# Task 003: Request Text eof Frame Merge

## 背景

当前 request text-frame path 中，client 用 `bot_a` 发送：

- 多条 `req` frame；
- 最后一条单独的 `req_end` frame。

server 收到 `req_end` 后，等待短时间以处理 Telegram 乱序，然后重建 request envelope。

Claude Code 大请求当前通常会触发 document fallback，因此本任务对 Claude Code 大请求收益有限。但对小请求、curl 测试和未触发 document fallback 的 request，去掉 `req_end` 可以减少一条 `bot_a` 消息。

## 决策

给最后一条 request text frame 增加 `eof=true`，去掉单独的 `req_end`。

示例：

```json
{"v":1,"rid":"r_x","seq":6,"kind":"req","total":7,"data":"...","eof":true}
```

server 收到带 `eof=true` 的 `req` frame 后，应使用该 frame 的 `total` 判断 expected chunks，并在收齐后处理 request。

## 重要边界

Telegram 消息可能乱序。最后一帧带 `eof=true` 不代表所有前序 frame 已经到达。

server 需要保留当前 `req_end` 路径里的“短暂等待乱序 frame”逻辑：

- 记录 expected total；
- 如果还没收齐，等待一小段时间；
- 收齐后重建；
- 等待后仍缺失时，按当前不完整请求处理策略记录 warning。

## 与 document fallback 的关系

document fallback 不受本任务影响。

当 request envelope 触发 document fallback 时，client 仍发送一条 Telegram document message，server 通过 caption 里的 metadata 识别和解码完整 request。

本任务只影响 text-frame request。

## 需要改动的区域

重点文件：

- `client/main.py`
- `server/relay.py`
- `shared/framing.py`
- tests under `tests/`

client 当前 `_send_envelope()` 在 text-frame path 里先发所有 `req`，再发 `req_end`。改造后：

- 最后一条 `req` frame 带 `eof=true`；
- 不再发送 `req_end`；
- 保留 document fallback 逻辑不变。

server 当前 `_handler()` 只把 `req` 和 `req_end` 交给 `_on_req_frame()`。改造后：

- `req` frame 中如果 `eof=true`，触发 reassembly；
- 继续兼容旧 `req_end`。

## 兼容性

server 应同时兼容：

- 旧协议：`req` + `req_end`
- 新协议：最后一个 `req eof=true`

client 新逻辑可以只发送新协议，但 tests 应覆盖 server 对旧协议的兼容。

## 注意事项

- 最后一帧带 `eof=true` 后，仍然应保留 `total` 字段。
- 如果只发送一个 text frame，该 frame 同时是 `seq=0` 和 `eof=true`。
- 不要影响 request document fallback 的 caption/hash/sha256 校验。
- 不要把 `eof=true` 加到 document fallback caption；document 自身已经表示完整 envelope。

## 验收标准

- text-frame request 不再发送单独 `req_end`。
- server 能从最后一个 `req eof=true` 重建 request。
- server 仍能处理旧的 `req_end`。
- 乱序场景下，server 会等待缺失 frame。
- document fallback 行为不变。
- tests 覆盖：
  - single-frame request with eof；
  - multi-frame request with eof；
  - eof frame 先到、前序 frame 后到；
  - old `req_end` compatibility；
  - document fallback unaffected。

## 非目标

- 本任务不优化 Claude Code 大 request 的 document fallback。
- 本任务不实现 DB cache。
- 本任务不处理 response path 的 `resp_end`；那是 Task 002。

