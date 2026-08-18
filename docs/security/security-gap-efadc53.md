# 安全差距基线（efadc53）

本文是后续开发的**唯一可信事实源**。只描述独立 clone 上的
`efadc53e7281ea86e962e4bc2028a36ff688f8df`（标签
`multi-bot-v0.3-paper-closeout`）。任何 worktree、未提交草稿、审计 clone
里的实验文件都不是基线。

冻结日：2026-08-18。

## 结论

`efadc53` 可以作为可信的 Paper / Shadow 基线；Paper 平台可投入内部使用，
`live` 继续硬禁用。实盘门禁仍是：按服务实现的 Transactional Outbox、
不可伪造身份、持久化架构、部署安全、真实 Shadow 验证。

## 基线核验

| 检查 | 结果 | 证据 |
| --- | --- | --- |
| 独立 clone | 通过 | `git clone https://github.com/xqw1377-prog/DSH-bot.git` 到 `/Users/xinquanwang/dsh-bot-security-audit-20260818`，不是旧 worktree |
| HEAD | `efadc53e7281ea86e962e4bc2028a36ff688f8df` | Merge pull request #6 into main |
| `git status --porcelain` | 空 | 无已跟踪脏文件 |
| `git fsck --full` | 无错误 | 对象完整 |
| `75cf04c` 是祖先 | 是 | `75cf04c` = 确定性事故闭环：Incident Center + Chief 幂等转发（PR #8） |
| 标签 | `multi-bot-v0.3-paper-closeout` | 指向同一 merge commit |

远端（本环境曾用 API 核验；读者若打不开 GitHub，以本表为据）：

- PR #6 已合并，`merge_commit=efadc53`
- 标签 https://github.com/xqw1377-prog/DSH-bot/releases/tag/multi-bot-v0.3-paper-closeout
- CI https://github.com/xqw1377-prog/DSH-bot/actions/runs/32042720054
  backend / frontend / smoke 均为 success

本地在该 SHA 上另跑：schema 检查、186 pytest、前端 lint / test / build。
`scripts/smoke_p0.sh` 以远端 CI 为准。

旧路径 `/Users/xinquanwang/DSH bot` 已消失，不能再对它做 worktree。
审计 clone 曾被写入未合入草稿，已 `reset --hard` + `clean`；那些文件不在本基线。

## Schema 数字含义

`scripts/check_schemas.py` 在该 SHA 的输出应读作：

```text
enum事件：38
payload schema：38
Runtime实际发射：15
已定义但尚未发射：23（WARN）
```

`OK: enum=38 schemas=38 runtime_emitted=15` 表示契约与 schema 对齐。
23 个 WARN 是**已有 payload schema、Runtime 尚未 emit** 的规划事件，
不是「23 个事件缺少 Schema」。

## 该 SHA 上已具备

- 本地 SQLite 事务：支持**单机、本地文件系统、多进程 worker**
  （`test_multiworker_idempotency.py` + WAL + busy_timeout + `BEGIN IMMEDIATE`）。
  **不支持**多节点共享同一数据库，也**不支持** NFS/SMB 上的 SQLite。
- 幂等键 + 查询认领：`STRONG` 才允许「查无」后释放；`EVENTUAL` 保持
  `SUBMISSION_UNKNOWN`。
- 成交后严格对账：mismatch 失败关闭，不猜仓。
- `live` 启动拒绝：`TradeExecutionCore` 与 runner 在 `mode=live` 时抛错。
- 失败关闭：无适配器 503；风控不可达拒单；Kill Switch 恢复前禁止新提交。
- Gateway API Key：`QUANT_GATEWAY_API_KEYS`；未配置且非 `development` 则拒启。

## 该 SHA 上未实现

- **Transactional Outbox**（P0）：`EventLog.emit` 独立 `INSERT` 后立刻
  `commit`，与任务状态不同事务。`execution.py` 把 outbox 列为 live 前置条件。
- **不可伪造用户身份**（P0）：写 BFF 生产依赖 `DSH_SESSION_USER` 环境变量。
- **PostgreSQL / 跨机单写约定**：各服务独立 SQLite 文件。
- **审计日志防篡改与异地只追加留存**。
- **部署层 TLS / Ingress / 网络策略**：UNASSESSED。应用仓库没有这些配置，
  不能从应用层缺失反推生产无 TLS。

## Outbox：按服务分别实现

不要做全局 Outbox。每个权威状态服务在**自己的数据库事务**里写自己的 Outbox：

| 服务 | 与状态同事务的事件 |
| --- | --- |
| Runtime | 任务状态与 `bot/task.*`（以及该任务步骤上的领域事件） |
| Gateway | 订单、审批、Kill Switch 与资金审计事件 |
| Incident Center | 事故状态与时间线事件 |
| Strategy Evolution | 实验、候选、晋级事件 |

投递语义是**至少一次（at-least-once）**，不承诺 Exactly Once。
消费者必须按事件 ID 幂等。

验收必须覆盖：

1. 状态提交后、发布前崩溃 — 重启后事件最终送达。
2. 发布成功后、标记完成前崩溃 — 允许重复投递，消费者不重复产生副作用。
3. 同一 aggregate 的事件顺序稳定。
4. 发布失败有退避和最大重试。
5. 毒消息进入 DLQ 并产生事故。
6. 支持按事件 ID 安全重放。

开发必须从 `efadc53` / 最新 `main` 开独立 feature 分支和 worktree，
**不要**在审计 clone 里改代码。

## 身份：优先 IAP / 反向代理

当前内部量化平台不先自建完整账号系统。

```text
用户 → SSO/IAP → Next BFF → 内部服务
```

要求：

- IAP 签发不可伪造 principal。
- 反向代理删除外部传入的身份 Header，再重新注入。
- BFF 只信任来自受控代理的身份。
- 审批、紧急停止、恢复动作绑定 principal 和角色。
- 内部服务继续使用独立 service identity / API Key。
- 未经 IAP 的生产写请求失败关闭。

若未来有外部用户、多租户或复杂权限，再升级应用内 RBAC。

## 四个独立里程碑

1. [Transactional Outbox](https://github.com/xqw1377-prog/DSH-bot/milestone/2) — 四个权威服务各自达标，六条验收全过。
2. [Identity/IAP](https://github.com/xqw1377-prog/DSH-bot/milestone/3) — 生产写路径只接受受控代理注入的 principal。
3. [Production Deployment/TLS](https://github.com/xqw1377-prog/DSH-bot/milestone/4) — 部署拓扑、TLS 终止、网络策略可评级。
4. [Crypto Read-only Shadow](https://github.com/xqw1377-prog/DSH-bot/milestone/5) — 真实只读接口 + 连续 Shadow，仍不 `request_order`。

既有 [Production Readiness](https://github.com/xqw1377-prog/DSH-bot/milestone/1) 仍是总览，不再把上述四项挤在同一个里程碑里。

完成以上之前不得打开 `live`。
