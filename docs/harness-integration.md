# DeepSeek Harness 接入兼容层方案

> 状态：规划。当前 `packages/dsh-runtime` 是自建「DSH 风格」最小运行时，
> **不构成「基于 DeepSeek Harness 构建」的宣称依据**。本文档定义接入路径；
> 在完成接入验证前，对外表述只能使用「DSH Bot（自建兼容运行时）」。

## 原则

1. **禁止重写**现有 Quant Gateway、风控、审批与执行闭环——它们已通过
   多轮验收测试，是确定性安全边界。
2. Harness 接入只替换「Agent 生命周期宿主」：Session 管理、插件装载、
   调度触发、记忆与事件的外围容器。
3. LLM 只负责解释、查询和任务拆解；不进入审批决定、硬风控与订单提交
   的确定性路径（现有红线代码不因接入而放松）。

## 兼容层设计

新增 `packages/dsh-harness-adapter`（唯一改动面），实现两个方向的适配：

### 1. dsh-runtime → Harness（宿主替换）

| dsh-runtime 概念 | Harness 对接点 | 适配方式 |
|---|---|---|
| `Profile`（能力声明） | Harness 插件 manifest / 权限声明 | 转换器：profile.yaml → manifest |
| `Agent.tick(session)` | Harness 插件生命周期回调 | `HarnessAgent` 包装 tick 为 cron/事件触发 |
| `BotSession`（记忆/事件/任务） | Harness session 与存储 | 适配器模式：`TaskStore`/`Memory`/`EventLog` 保留 SQLite 实现，Harness 只拿到只读视图 |
| `run_forever` 调度 | Harness scheduler | 删除自建循环，注册为 Harness 定时任务 |

### 2. Harness → 现有插件（协议保留）

现有插件（crypto-agent、market-chief、trade-approval）的入口协议
`Agent.tick(session)` 不变；兼容层提供 `session` 的 Harness 实现，
保证 `session.use()` 能力检查、任务状态机、事件发射语义完全一致。

## 接入验证清单（未全部通过前不算接入完成）

- [ ] 依赖声明：`packages/dsh-harness-adapter` 依赖原始 Harness 包（非 fork）
- [ ] 启动入口：`run_dsh.py` 经 Harness 启动，无自建调度循环存活
- [ ] 插件生命周期：装载/卸载/崩溃重启由 Harness 管理
- [ ] 现有全部验收测试（执行语义/幂等/对账/只读 Chief）在 Harness 宿主下原样通过
- [ ] 审计对照：接入前后 `/v1/audit` 事件序列一致
- [ ] LLM 沙箱：LLM 调用无交易密钥、无 Gateway write scope 凭据

## 回退

兼容层失败时删除 `packages/dsh-harness-adapter` 并恢复
`run_dsh.py` 使用 `dsh-runtime.run_forever`，其余代码零改动。

---

## 附：SQLite 持久化能力边界（当前声明）

**支持**（已由测试验证）：
- 单机部署，uvicorn 多 worker（独立连接 + WAL + busy_timeout +
  `BEGIN IMMEDIATE` 事务抢占 + PRIMARY KEY 唯一约束）
- 本地 Paper / Shadow 环境的全部闭环（审批、幂等、审计、崩溃恢复）

**不支持 / 未验证**：
- 多节点 Gateway 同时写入同一数据库
- 共享网络文件系统（NFS/SMB）上的 SQLite（文件锁语义不可靠）
- 跨机高可用故障切换

**真实资金阶段要求**（二选一）：
1. 迁移到 PostgreSQL（存储层 `storage.py` 单文件替换，SQL 方言差异小）；
2. 或明确约定 Gateway 保持单实例写入（前置负载均衡只做读分流）。

在上述任一条件满足前，系统只能用于 Paper/Testnet。

## 实盘前 P0 清单

1. **事务 Outbox**：任务状态与待发送事件必须在同一数据库事务提交，
   后台可靠发布。当前顺序（先持久化任务状态、后发射事件）在 Paper
   场景安全（事件失败不丢任务），但事件本身可能丢失；
   实盘前必须升级为 Outbox 模式。
2. PostgreSQL 迁移或 Gateway 单实例写入锁定（见 SQLite 边界附录）。
3. 多节点部署与故障切换验证。
