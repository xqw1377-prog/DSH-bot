# Runtime Local Transactional Outbox

范围：`packages/dsh-runtime` 自己的 SQLite。**不是**全系统 Outbox。

未覆盖：Quant Gateway、Incident Center、Strategy Evolution、跨服务总线。
`live` 继续硬禁用。

## 行为

- 任务状态与待发事件进入同一事务的 `event_outbox`。
- 执行核通过 `TaskStore.transition_with_event(...)` 绑定额外领域事件，
  禁止 `transition()` 后再单独 `EventLog.emit()`。
- 发布器对每一行 `BEGIN IMMEDIATE` 抢占 `PENDING`，`INSERT OR IGNORE`
  写入 `domain_events`，再标记 `PUBLISHED`，同一事务提交。
- 投递语义：至少一次。`event_id` 唯一。消费者仍需幂等。

## 指标（查询接口）

- `outbox_pending_count`
- `outbox_oldest_pending_seconds`
- `outbox_failed_count`

见 `dsh_runtime.outbox_metrics()`。
