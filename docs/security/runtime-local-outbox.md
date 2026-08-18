# Runtime 本地 Transactional Outbox

本文说明 `packages/dsh-runtime` 自己的 SQLite 如何把任务状态与领域事件放进同一事务。
**这不是全系统 Outbox。** Gateway、Incident Center、Strategy Evolution
仍按各自的存储提交；本文不声称生产投递或跨服务可靠发布已经完成。

基线缺口见 `docs/security/security-gap-efadc53.md`（历史审计快照，不要改）。

## 范围

- 业务状态（`bot_tasks`）与 `event_outbox` 行在同一 SQLite 事务中提交。
- Publisher 至少一次把 outbox 行写入 `domain_events`（`INSERT OR IGNORE`）。
- 仅覆盖 Runtime 进程本地文件库。SQLite 是单机、本地文件系统、多进程 worker；
  不支持多节点共享文件系统。
- `live` 仍然启动失败。本模块不打开实盘。

## 不在范围

- Quant Gateway 自己的 Outbox / 幂等键
- 身份认证、IAP、OIDC、写 BFF 的 `decided_by`
- Incident Center 服务实现（毒消息只在 Runtime 本地开 `incident/opened`）
- 日志框架、审计系统、Strategy Evolution

## 提交路径

1. `EventLog.emit` 先做 payload schema 校验，然后只 `INSERT event_outbox`。
   校验失败或 Outbox 表不存在时，禁止直写 `domain_events`。
2. `TaskStore.create` / `transition` / `transition_with_event` 走 `transaction()`：
   任务行与 `bot/task.*`（以及调用方给出的领域事件）同一事务。
3. 最外层 `commit` 之后才 `publish_outbox()`。
4. `run_once` 的 `finally` 再排空一次，避免 tick 内残留 PENDING。

执行核里「改任务 + 发领域事件」必须走 `transition_with_event`，
禁止 `transition()` 后再单独 `emit()`。

## Publisher

- 用 `BEGIN IMMEDIATE` 抢占 `PENDING` 或 lease 过期的 `CLAIMED` 行。
- 同一 `aggregate_id` 按 `sequence` 投递；未完成的 seq N 会挡住 seq N+1。
- 写入 `domain_events` 与标记 `PUBLISHED` 分两段提交，以便测试
  「发布可见、ack 未完成」：重启后 `INSERT OR IGNORE`，不产生重复副作用。
- 失败按次数退避；达到上限进入 `event_outbox_dlq`，行标 `FAILED`，
  并入队本地 `incident/opened`。`FAILED` 解除对该 aggregate 后续 sequence 的永久阻塞。
- `event_consumption(event_id, consumer)` 支持按 `event_id` 安全重放。

## 指标

`outbox_metrics()` 返回：

- `outbox_pending_count`
- `outbox_oldest_pending_seconds`
- `outbox_failed_count`

## 迁移

旧 SQLite 没有 `event_outbox` 时，`_connect()` 会建表。已有 `bot_tasks` /
`domain_events` 行保持可读；新事件必须走 Outbox，不能静默回退直写。
