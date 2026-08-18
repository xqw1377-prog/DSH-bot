# Runtime 本地 Transactional Outbox

本文说明 `packages/dsh-runtime` 自己的 SQLite 如何把任务状态与领域事件放进同一事务。
**这不是全系统 Outbox。** Gateway、Incident Center、Strategy Evolution
仍按各自的存储提交；本文不声称生产投递或跨服务可靠发布已经完成。

基线缺口见 `docs/security/security-gap-efadc53.md`（历史审计快照，不要改）。
合并后另写 `docs/security/deltas/runtime-outbox-<merge-sha>.md`。
Transactional Outbox 里程碑在 Runtime 合并后保持开启。

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
- 日志框架、全系统审计、Strategy Evolution

## 提交路径

1. `EventLog.emit` 先做 payload schema 校验，然后只 `INSERT event_outbox`。
   校验失败或 Outbox 表不存在时，禁止直写 `domain_events`，
   也不能静默退回「`transition()` 后再单独 `emit()`」。
2. `TaskStore.create` / `transition` / `transition_with_event` 走 `transaction()`：
   任务行与 `bot/task.*`（以及调用方给出的领域事件）同一事务。
3. 最外层 `commit` 之后才 `publish_outbox()`。
4. `run_once` 的 `finally` 再排空一次，避免 tick 内残留 PENDING。

执行核里「改任务 + 发领域事件」必须走 `transition_with_event`。

## 顺序

`UNIQUE (aggregate_id, sequence)`。同一 task（`payload.task_id`）按 1 → 2 → 3 投递。
seq N 仍为 PENDING/CLAIMED 时，seq N+1 不可发布。seq N 进入 `DEAD` 后解除阻塞。
不同 `aggregate_id` 互不阻塞。

## Publisher 崩溃恢复

认领会先提交 `CLAIMED`（PROCESSING）和 `locked_until`（lease），
然后另开短事务写入 `domain_events`，再另开短事务标记 `PUBLISHED`。
因为存在已提交的 PROCESSING 行，**必须**用 lease 过期恢复，不能省略。

约束：

- Publisher 不得在事务外做网络 / 下单 / 外部 IO。本地 SQLite 读写除外。
- `locked_until` 未过期时，其他 Publisher 不得抢占。
- lease 过期后，其他 Publisher 用 `BEGIN IMMEDIATE` 接管；
  `INSERT OR IGNORE` 保证 `domain_events` 不出现第二条。

测试注入 `crash_after_publish` 只用于模拟「`domain_events` 已可见、ack 未完成」的进程死亡。

## 退避与 DLQ

- 每次认领 `attempts += 1`。
- 失败后 `available_at = now + min(2^attempts, 60)s`。
- 达到 `MAX_ATTEMPTS`（5）进入 `DEAD`，并写入 `event_outbox_dlq`。
- 一条毒消息不能永久阻塞其他 aggregate；同 aggregate 的后续 sequence 在本条 `DEAD` 后可继续。
- `outbox_failed_count` 等于 `status = 'DEAD'` 的行数。

## 安全重放

按 `event_id` 重放：

- `domain_events` 用 `INSERT OR IGNORE`，不出现第二条。
- `event_consumption(event_id, consumer)` 使消费端第二次返回 False。
- 每次重放写入 `event_replay_audit`。

## 指标

`outbox_metrics()` 返回：

- `outbox_pending_count`
- `outbox_oldest_pending_seconds`
- `outbox_failed_count`

## 迁移

旧 SQLite 没有 `event_outbox` 时，`_connect()` 会建表。已有 `bot_tasks` /
`domain_events` 行保持可读；新事件必须走 Outbox，不能静默回退直写。
若旧草稿列名是 `next_attempt_at` / `lease_until` / `FAILED`，启动时迁到
`available_at` / `locked_until` / `DEAD`。
