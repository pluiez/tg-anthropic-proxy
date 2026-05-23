# Task 005: Future Hybrid Response Document Fallback

## 背景

当前 response path 只使用 Telegram text frame：

- 每个 `resp_chunk` payload 会 gzip/base64；
- frame 作为 Telegram text message 发送；
- 不会像 request path 那样打包成 document。

理论上 response 也可以加 document fallback，但它不是当前第一优先级。

## 当前取舍

不优先做 response document streaming fallback，原因：

- document 本身仍然是一条 Telegram 消息；
- document 上传、下载、解压都有额外成本；
- 如果每秒发送一个 document 来模拟 streaming，仍然可能撞上同一个 channel 的发送限制；
- 小 response 走 text frame 更简单、更低延迟；
- 当前日志中多数 response 已经只有 2 条 `resp_chunk`，response path 不是主要瓶颈。

更合理的长期方向是 hybrid：

- 小 response chunk 继续走 text frame；
- 只有当某个 flush 后的 response chunk 会拆成很多 text frame 时，才考虑打包成 document。

## 可能的触发条件

未来如果实现，可以考虑配置：

```dotenv
PROXY_RESPONSE_DOCUMENT_CHUNK_THRESHOLD=8
PROXY_RESPONSE_DOCUMENT_MIN_BYTES=32768
```

含义：

- 如果某次 response flush 动态切分后会产生 >= N 条 text frame；
- 或 raw response bytes 超过某个阈值；
- 则改用 document fallback。

不要对小 response 使用 document fallback。

## 协议草案

server 发送 response document message，caption 类似 request document：

```json
{
  "v": 1,
  "rid": "r_x",
  "kind": "resp_blob",
  "seq": 3,
  "encoding": "gzip",
  "raw_size": 123456,
  "sha256": "..."
}
```

client 收到 document 后：

1. 下载 document；
2. gzip 解压；
3. 校验 raw_size 和 sha256；
4. 将 bytes 作为 SSE payload yield 给 Claude Code；
5. 如果 caption 有 `eof=true`，结束 stream。

## 风险

- document 下载会增加端到端延迟。
- document 不是天然 streaming，每个 document 内部 payload 要等完整下载后才能输出。
- 需要 client 监听 document update；当前 client listener 只处理 text frame。
- Telegram document 上传 timeout 和 orphan 问题会扩散到 response path。
- 如果 response document 和 text response frame 混用，需要严格维护 seq 顺序。

## 建议前置条件

在考虑本任务前，先完成：

1. DB cache and remove `cache_ack`；
2. response `eof=true` 合并；
3. document timeout/orphan/cancel 修复。

否则 response document fallback 会把已有 request document 的不稳定性带到 response path。

## 验收标准

这是未来任务，目前不建议立即实现。若后续实现，至少需要：

- 小 response 不使用 document fallback；
- 大 response chunk 可减少 text frame 数量；
- client 能正确按 seq 输出 text frame 和 document blob；
- document blob 校验失败时返回明确错误；
- 流式语义没有明显倒退；
- tests 覆盖 text/document 混合顺序、eof、校验失败、fallback threshold。

