# DeepSeek Harness 兼容性 Spike 报告

> 状态：**已完成**（结构验证 + keyless 完整回合均通过，可复现：
> `bash tools/harness-spike/verify.sh <harness-checkout>`）。
> 结论：**建议接入，但仅作为隔离的 LLM 宿主层**，锁定 commit、保留回退。

## 锁定版本

| 项 | 值 |
|---|---|
| 仓库 | https://github.com/deepseek-ai/deepseek-harness |
| 锁定 commit | `47f943859bef`（2026-08-13，master） |
| 许可 | MIT |
| 官方状态 | Developer Preview，**明确会有破坏性变更** |
| 运行要求 | Node 22.19+/24+，pnpm 11.7.0（Corepack） |

Spike 检出不进入本仓库；`tools/harness-spike/verify.sh` 负责在锁定
checkout 上复现验证。

## 架构测绘（基于锁定 commit 的 docs/ 与 packages/）

- Cordis 插件树：一切皆插件（模型适配器、工具注册表、agent loop 本身），
  无特权核心；注册即效应，卸载自动回滚
- Profile = 命名组合（bundles 叠加 + cordis.patch.yml）；bundle 是可分发
  配置层；`--patch` 可插入 out-of-tree 插件（`name: './path/plugin.ts'`）
- 会话：append-only `SessionEvent` 日志（事件词表可经 declaration merging
  扩展）——与我们的 EventLog 语义同构
- 工具：`defineTool` + `ctx.tools.register`，注册表按 allowlist 生成模型
  面 schema；执行管道有 pre/post-execute 守卫
- Schedule：**会话内、模型管理的提醒**（schedule_create 等工具），不是
  确定性外部定时器 → 我们的 Bot tick 不能依赖它（见发现 3）

## DSH Bot → Harness 映射

| 我们的概念 | Harness 对应 | 状态 |
|---|---|---|
| Profile（能力声明） | profile/bundle + 工具 allowlist | 映射清晰 |
| Agent.tick（确定性） | 无直接对应（Harness 面向 LLM 回合） | 外部触发 headless 运行 |
| EventLog（34 类事件） | SessionEvent（可扩展词表） | 语义同构，需桥接 |
| Memory（SQLite） | Session 日志派生 | 需桥接导出 |
| run_forever 调度 | 无（Schedule 为模型内提醒） | 保留我们的调度器 |
| Gateway 只读工具 | defineTool 注册 | 直接映射（本次验证） |

## 六项验收清单与结果（实测）

1. **锁定 commit/版本**：✅ `47f943859bef`，verify.sh 启动即强校验
2. **只读 Chief 插件加载**：✅ 生命周期 `apply()` 日志出现于真实启动；
   插件树加载失败会阻断启动（实测：schema 错误导致整树拒绝加载，
   修复后通过——证明 Harness 对插件是强校验而非静默容忍）
3. **Profile 能力映射**：✅ 工具注册表 allowlist 机制与 primary_tools
   语义对齐；插件只注册 3 个只读工具
4. **Session/生命周期回调**：✅ keyless 完整回合（llm-replay）中，
   `tool/result` 持久化进 append-only 会话日志（zstd），含
   `readonly_guaranteed:true` 与 `write_tool_violations:[]`
5. **Schedule 触发**：❌ 不适用——Harness Schedule 是模型管理的会话内
   提醒，确定性 tick 必须保留我们的调度器（架构发现，非缺陷）
6. **事件与记忆桥接**：可行性实证——SessionEvent 日志（append-only、
   持久化、可 zstd 读回）与我们的 EventLog 同构；词表合并桥接
   留接入阶段实现

## 验证方法与坑（复现要点）

- 插件目录需 `package.json {"type":"module"}`，否则 tsx 按CJS require
  触发 ESM 循环错误
- `--patch` 中插件路径必须是绝对路径（相对路径按 profile home 解析）
- `defineTool` 的 output.schema 必须显式 `additionalProperties`
- 顶层 patch 的同 id 行只覆盖 config，不改 name——替换插件须
  `disabled: true` + `insert`（本轮替换 llm-deepseek 即用此法）
- 会话标题生成器会消耗一次模型调用，keyless 回放需禁用
  `session-title-llm` 并为脚本加保险条目

## 安全红线验证

- 只读插件无凭据：Config 仅 gatewayUrl（只读 REST）
- `chief_readonly_audit` 工具在运行时断言注册表中不存在任何写操作工具
  （request_order/cancel_order/decide_approval/emergency_stop 等）
- 本 Spike 对现有 Gateway、TradeExecutionCore、风控零改动（见
  「零改动证明」）

## 发现

1. **Developer Preview 风险真实**：4 天内多个合并，破坏性变更承诺 →
   任何接入必须锁 commit 并在我们的仓库内 vendored/固定，不追新
2. **LLM 中心 vs 确定性中心**：Harness 的回合模型（turn/step、agent
   loop）面向模型驱动；我们的执行闭环是确定性状态机 → 接入定位应是
   「Harness 宿主 LLM 解释/任务拆解层，我们的 Runtime 保留确定性调度
   与执行」，而非整体替换
3. **Schedule 语义错配**：确定性 Bot tick 不能用 Harness Schedule
4. 无 LLM key 也能完成结构性验证（dump-config）；完整回合需
   llm-replay（keyless 回放，接入阶段采用）

## 结论与建议

**建议接入**，定位为「Chief 的 LLM 宿主层」，且必须：

1. 锁定 commit（`47f943859bef`），不追 master（Developer Preview，
   4 天内多次合并，破坏性变更承诺）
2. 隔离接入：`DSH_HARNESS=1` 时 Chief 的解释/查询/任务拆解回合走
   Harness；确定性调度与执行闭环保留在自建 Runtime；回退 = 删环境变量
3. 现有 Gateway、TradeExecutionCore、风控零改动（本 Spike 已按此红线
   实证）
4. 接入阶段补：SessionEvent ↔ EventLog 桥接、Memory 导出、
   凭据仅限 LLM API key（经 Harness credentials 服务，绝不进入插件）

## 零改动证明

Spike 全部工件位于 `tools/harness-spike/`（一个 TS 插件、一个 patch、
一个验证脚本）与本文档；`git diff origin/main -- services/ packages/
plugins/` 为空即证明。
