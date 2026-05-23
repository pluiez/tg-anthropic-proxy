# Task 001: DB Cache And Remove cache_ack

## 背景

当前协议级 cache 是内存 cache。`server` 在处理完整 request 后，把 `tools`、`system`、`messages` 前缀等大块内容存入内存，并通过 `cache_ack` 告诉 `client`：“这些 key 已经在 server 侧缓存成功，下次可以只发 hash ref。”

这个机制已经证明可以减少 request envelope 大小，但仍有几个问题：

- cache 只在进程内存里，client/server 任意一边重启后状态就不一致。
- `cache_ack` 本身需要 Telegram 消息，会增加 `bot_b` 的发送量。
- client 必须等 server ack 才敢用 ref，协议状态更复杂。
- server DB 丢失或机器切换时，client 可能长期相信旧的 server cache 状态。

本任务目标是把 cache 改成本地 DB，client/server 各自用本地 DB 管理 cache；协议里只传 hash ref，不再依赖 `cache_ack`。

## 决策

使用本地 DB cache，优先用 Python 标准库 `sqlite3`，不要新增依赖。

cache key 直接使用 canonical JSON value 的 SHA-256 hex digest：

- 新 key 格式：`<64 hex chars>`
- 不再使用当前的 `sha256:<64 hex chars>` 前缀。

cache value 存 canonical JSON bytes，而不是 Python object。这样 hash、落盘内容、重建行为都基于同一份 canonical bytes。

request envelope 顶层必须携带整数 `cache_ts`，用于同步 cache 的 `last_accessed_at`。建议使用 Unix epoch milliseconds：

```json
{
  "path": "/v1/messages",
  "headers": {},
  "cache_ts": 1779600000000,
  "body_json": {}
}
```

`cache_ts` 放在 envelope 顶层，而不是放在最后一个 frame 中。这样 text frame 和 document fallback 都天然兼容。

`created_at` 不需要 client/server 同步，可以使用本机当前时间。`last_accessed_at` 必须用同一个 `cache_ts` 同步，并且更新时使用：

```text
last_accessed_at = max(existing_last_accessed_at, cache_ts)
```

这样可以避免并发请求或 Telegram 乱序导致访问时间倒退。

## 新配置

在 `.env.example` 中加入：

```dotenv
PROXY_CLIENT_CACHE_DB_PATH=.cache/client-cache.sqlite3
PROXY_SERVER_CACHE_DB_PATH=.cache/server-cache.sqlite3
PROXY_CACHE_CLIENT_HIT_SERVER_MISS_MAX_REPLAYS=10
```

如使用 `.cache/` 作为默认目录，需要确保不会被 git 跟踪。仓库当前如果没有 `.gitignore`，本任务应新增或更新 `.gitignore`，至少忽略：

```gitignore
.cache/
*.sqlite3
*.sqlite3-shm
*.sqlite3-wal
```

## Miss Replay 规则

`PROXY_CACHE_CLIENT_HIT_SERVER_MISS_MAX_REPLAYS` 是 client 进程运行期间的累计计数，不是连续计数。

判断条件：

1. client 本地 DB 命中，所以发出了 cache ref；
2. server 返回 `cache_miss`；
3. client 记录一次 `client cache hit but server cache miss`；
4. client 进程内累计计数加一；
5. 如果累计次数小于等于配置值，client 用同一个 `cache_ts` 重放完整请求；
6. 如果累计次数超过配置值，client 报错退出，不再重放。

client 进程重启后，累计次数重置。

错误文案要明确说明 cache 状态不一致，并给出操作建议：

```text
client/server cache inconsistency detected: client cache refs missed on server too many times.
Increase PROXY_CACHE_CLIENT_HIT_SERVER_MISS_MAX_REPLAYS if the server cache was intentionally reset, or clear the client cache DB.
```

## 四种 cache 状态

client 有、server 没有：

- client 发 ref；
- server 返回 `cache_miss`；
- client 按上面的累计 replay 规则判断是否允许重放；
- 允许时使用同一个 `cache_ts` 重放完整 request；
- server 收到完整内容后写入自己的 DB。

client 没有、server 有：

- client 本次发完整内容；
- client 同时写自己的 DB；
- server 发现已有内容，只更新 `last_accessed_at = max(existing, cache_ts)`。

两边都有：

- client 发 ref；
- server restore 成功；
- 两边都 touch 同一个 `cache_ts`。

两边都没有：

- client 发完整内容并写自己的 DB；
- server 从完整内容创建 DB entry；
- 两边用同一个 `cache_ts` 作为 `last_accessed_at`。

## 需要改动的区域

重点文件：

- `shared/cache_protocol.py`
- `shared/cache_store.py`
- `client/main.py`
- `server/relay.py`
- `.env.example`
- `README.md`
- tests under `tests/`

建议新增：

- `shared/cache_db.py` 或类似模块，封装 SQLite 操作。

保留 `shared/cache_protocol.py` 作为协议层：canonical JSON、hash、ref 包装、restore/compress 逻辑应继续放在协议层。

## cache_ack 删除方式

DB cache 生效后，server 不再发送 `cache_ack`。

client 不再依赖 `cache_ack` 判断 server 是否拥有 key，而是：

- 本地 DB 有 key，就可以尝试发 ref；
- server 没有时通过 `cache_miss` 修复；
- `cache_miss` 超过累计上限时停止，提示 cache 不一致。

为了兼容旧日志或旧 frame，client 可以保留 `cache_ack` handler 一段时间，但新 server 不应继续发送 `cache_ack`。

## 注意事项

- DB 会持久保存 `system`、`tools`、`messages` 等内容，可能包含敏感上下文。默认路径必须避免被 git 提交。
- TTL 仍然应保留，默认 72 小时。DB 清理可以在启动时、写入时或固定间隔执行。
- 计算 hash 时必须使用 canonical JSON bytes，否则 client/server 独立计算出来的 hash 可能不一致。
- replay full request 必须复用同一个 `cache_ts`，否则 client/server 的 `last_accessed_at` 会出现不必要分叉。
- server 处理 `cache_miss` 时不要创建半成品 cache entry。
- cache key 从 `sha256:<hex>` 改成 `<hex>` 后，需要同步更新 tests。

## 验收标准

- client/server 可在无 `cache_ack` 的情况下完成缓存引用、restore、miss replay。
- client/server 重启后，本地 DB 中仍存在有效 cache entry。
- server DB 清空后，client 第一次发 ref 会收到 `cache_miss`，随后 full replay 可以修复 server DB。
- 超过 `PROXY_CACHE_CLIENT_HIT_SERVER_MISS_MAX_REPLAYS` 后，client 返回明确错误，不再无限 replay。
- `.env.example` 和 README 记录新配置。
- 单元测试覆盖：
  - canonical hash key 为 64 hex；
  - DB put/get/touch/TTL；
  - `last_accessed_at = max(existing, cache_ts)`；
  - server miss -> client replay；
  - replay 上限。

## 非目标

- 本任务不优化 response frame 消息数。
- 本任务不实现 response document fallback。
- 本任务不处理 Telegram `sendDocument` timeout/orphan；那是 Task 004。

