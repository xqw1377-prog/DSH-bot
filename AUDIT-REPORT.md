# DSH Bot 全面审计报告

> 审计日期：2026-08-19 ｜ 范围：全仓（services / plugins / packages / apps / infra / scripts / CI）
> 基线：README 自定位为 **paper-closeout（非实盘就绪，live 模式三层禁用）**
> 方法：4 路并行深审 + 关键发现逐条人工复现验证（测试 243 项、schema 检查、Dockerfile、多 worker 并发、端口泄漏等均已实测）

---

## 0. 验证结论摘要

| 验证项 | 结果 |
|---|---|
| 后端测试套件（干净环境） | ✅ **243/243 通过** |
| `scripts/check_schemas.py`（schema 契约检查） | ❌ **Windows 直接崩溃（GBK 编码 + 反斜杠路径）；Linux CI 上因 `market/chief.briefing` 未进 envelope 枚举而红** |
| CI smoke job | ❌ `pip install $PKGS` 未装 pytest，后续步骤却调用 `pytest`（ci.yml:37/49） |
| risk-policy Docker 镜像 | ❌ Dockerfile 只 COPY 服务本体，`pyproject` 依赖 `dsh-contracts` 未复制 → **构建必失败** |
| 多 worker 幂等测试 | ⚠️ Windows 上**泄漏僵尸进程**（uvicorn `--workers` 的子进程在父进程 SIGTERM 后存活并占住端口，后续运行 500/超时）；干净端口下逻辑本身通过 |
| live 禁用 | ✅ 三层确认：`execution.py` 抛 ValueError、两个 run 脚本 SystemExit、Gateway 无实盘 adapter（未注册市场 503） |

**一句话结论：交易资金路径的幂等/崩溃恢复/fail-closed 设计扎实、测试充分；但「审计-风控-晋级」治理链基本是形式化的（全是自报数据、不验证真实证据），且 CI 实际是红的、两个关键组件（风险审计器接线、risk-policy 镜像）在本地/生产脚本路径上是坏的。** 离 live 还有硬门槛，但方向正确。

---

## 1. 致命问题（Critical，实盘前必改）

### C1. 审批与订单不绑定，APPROVED 永不过期
- `services/quant-gateway/src/quant_gateway/routers/orders.py:288-307` — 下单唯一审批门禁是 `is_approved(order_intent.approval_id)`。
- `services/quant-gateway/src/quant_gateway/approval_store.py:116-126` — `_expired()` 只对 `REQUESTED` 生效。
- 后果：**一个 APPROVED 审批 ID 可以无限次、任意市场/任意标的/任意数量地下单，永久有效。** 这是纸面交易唯一真正致命的资金安全漏洞。
- 修复方向：审批绑定订单意图哈希（market/subject/symbol/side/qty 的规范化摘要）+ 过期时间 + 使用后作废（one-time use）。

### C2. 独立 Risk Auditor 双实现不兼容，本地/生产脚本接错了服务
- 两套实现：`services/risk-auditor/src/risk_auditor/main.py`（schema 匹配 evolution 调用方）与 `plugins/dsh-risk-auditor/src/dsh_risk_auditor/service.py`（schema 不匹配）。
- `scripts/start-local.sh:71` 与 `scripts/start-backends.sh:66` 都启动**插件版**（`dsh_risk_auditor.service:app`）。
- evolution 调用方（`strategy-evolution/src/strategy_evolution/main.py:250-260`）发送 `{candidate_id, market, strategy_id, ...}` 并期望 `{verdict, reason, conclusion_id}`；插件版接收 `{candidate: dict, evidence_refs, upstream_passed}` 并返回 `{approved, audit_id}`。
- 后果：**本地与生产脚本部署下，晋级到 APPROVED/CANARY/PRODUCTION 永远 422（"risk auditor rejected"）。** 只有 Docker compose（`risk_auditor.main:app`）路径可用。CI 集成测试直接 boot 了 services 版，未走真实 wiring，所以 CI 绿而真实路径坏。
- 修复方向：删除一套（建议删插件版，保留 services 版），统一 contract，并把 CI 的晋级集成测试改为走 `start-local.sh` 真实路径。

### C3. 风控数据全部自报、第二道防线不重算；权益异常静默归零
- `orders.py:57-64` — `_account_equity` 任何异常返回 `0`，risk-policy 把 `equity=0` 映射为 CRITICAL → 触发 kill switch：**一次瞬态适配器抖动就全局停摆。**
- `services/risk-policy/src/risk_policy/main.py:116-157` — 只对调用方自报的 `notional / worst_case_loss / equity` 做阈值检查，不读取持仓/账户真实状态。
- `packages/dsh-runtime/src/dsh_runtime/execution.py:392-393` — 缺省字段补 `"0"`，而 `0` 能通过所有风控检查：**缺字段即绕过（bypass by omission）。**
- `max_drawdown`（`main.py:29/38/44`）声明并加载，但任何检查都不引用 — 死配置。
- `max_position` 只对单笔 notional 生效，从不累计全局敞口。
- 修复方向：第二道防线必须从权威账户/持仓状态推导（而非信任提交方数字）；缺字段拒绝而非补 0；权益取不到应 fail-closed 拒绝订单而非 kill switch。

### C4. A 股整手/T+1 风控在 paper 模式被整体跳过
- `execution.py:825-826` — `_policy_risk_block` 中 `if self.mode != "shadow": return None`：paper 模式（默认）完全跳过整手、涨跌停、T+1 校验。
- `execution.py:790-797` — 信号缺 quantity 时 paper 模式回退 `"0.01"`，A 股可提交小数股订单（现有测试把 0.01 股下单断言为 DONE，已固化为行为）。
- `plugins/dsh-trade-core/policy.py` 的 `AStockMarketPolicy`（会话/整手/±10%/T+1）完整实现但**无人调用**（死代码）。
- 后果：纸面交易中 A 股规则名存实亡；shadow 与 paper 行为不一致（"shadow=paper 减去资金"不成立）。

### C5. 控制端点与取消下单绕过审批，审计 outcome 恒为 OK
- `orders.py:366-371` — `cancel_order` 只需 `write` scope：**任何 write 密钥可无审批取消任意订单。** 模块 docstring（orders.py:1-9）承诺 request/cancel 都必须满足审批+幂等+风控，代码不兑现。
- `routers/control.py:17-86` — `pause_strategy / resume_strategy / emergency_stop / resume_kill_switch` 全部只需 `write` scope，与 docstring（"必须走审批"）矛盾；kill-switch 恢复是最高影响动作却无人工审批。
- `audit.py:23` — 审计 `outcome` 恒为 `"OK"`，包括失败/拒绝动作，审计记录不可信。

### C6. 职责分离缺失：write 密钥可自批自单
- `auth.py:74-91` + `routers/approvals.py:72-87` — 任何 `write` scope 密钥既能下单也能审批。Bot 持 write 才能下单，故 Bot 密钥可审批自己的单。仅靠 Next BFF 的 `DSH_SESSION_USER` 在网关之外补偿，网关层无强制。
- 修复方向：引入独立 `approve` scope（仅人机交互 BFF 持有），网关层拒绝 bot 类 principal 决定审批。

---

## 2. 高危问题（High，生产前必改）

### H1. 无认证面扩散
- `services/incident-center` — 全端点无鉴权（`main.py:139-271`）：任何人可伪造 `HIGH` 事故、任意 actor 解析/关闭事故；指纹可被自由改动 `incident_type` 字符串拆分绕过去重。
- `services/strategy-evolution`、`services/risk-policy`、`services/risk-auditor` — 全部端点无鉴权。docs/security 的 endpoint-permission-matrix.md:121-129 要求的服务身份模型（BFF 专属 promote、审计员专属 audit）完全未落地。
- 后果：能访问 8002/8003/8005 的任何进程可自造实验/候选、自造 approval_id、自我晋级到 PRODUCTION。

### H2. 晋级治理链是形式化：证据不验证、approval_id 不验证、结论幂等键漏审批
- `risk-auditor/main.py:66-73` — 只数**去重后的字符串条数** ≥3，不解析 ref 是否对应真实实验/回测，无来源多样性校验 → 伪造 `["a:1","b:2","c:3"]` 即 PASS。
- `risk-auditor/main.py:72-73` — `approval_id` 只查非空，从不核对 gateway 审批账本是否真的 APPROVED。
- `risk-auditor/storage.py:67-82` — 结论幂等键是 `(candidate, to_stage, evidence_hash)`，**不含 approval_id**：一次 PASS 缓存在换 approval 后、甚至 approval_id=None 时都可重放；结论永不失效、无法撤销。
- 证据哈希算法在三个组件间有**两种不同 canonicalization**（json.dumps sorted vs 换行 join set），同一批证据在两处算出不同哈希，审计链断裂。
- 后果：号称的「≥3 独立证据 + 人工审批」红线是装饰性的，背后是无鉴权 HTTP 端点。

### H3. 多 worker 模式：订单号碰撞 + 运行库非多写安全
- `services/quant-gateway/src/quant_gateway/adapters/paper.py:48,175` — `self._ids = count(1)` 每进程实例一个；两 worker 并发提交不同订单时产生相同 `CRYPTO-paper-N`，`storage.py:166-176` 的 `ON CONFLICT(order_id) DO UPDATE` 会**静默覆盖**前一笔记录。现有多 worker 测试只发一笔单，未覆盖碰撞。
- `packages/dsh-runtime/src/dsh_runtime/store.py:19-75` — 无 WAL、无 busy_timeout、进程级全局 `_conn`/`_tx_depth`；`tasks.py:151-172` 任务状态转换是无乐观锁的 read-modify-write。README 宣称多 worker 支持，但多进程并发写同一 runtime.db 会 `database is locked` 与 MAX(sequence) 竞争。
- 实测：Windows 上 `--workers 2` 的子进程在父进程 SIGTERM 后成为僵尸并占住端口，后续测试运行收到 500/超时；CI 在 Linux 上掩盖了该问题。
- 修复方向：paper adapter 的 order_id 用 uuid 前缀或 `os.getpid()` 加进计数；runtime 库开 WAL + busy_timeout + 任务级乐观锁（version 列）。

### H4. SQLite 默认 `:memory:`，生产忘设 env 即丢全部账本
- `storage.py:37`（gateway）、`strategy-evolution/storage.py:26`、`incident-center/main.py:67`、两个 auditor、runtime `store.py:20` 全部默认内存库。
- 生产部署若漏设 DB env 变量，重启即丢审批账本、幂等键日志、候选/证据账本。gateway 自己的注释（storage.py:1-5）明确说「审批账本与幂等键日志不允许仅存内存」，与默认值自相矛盾。**实盘下丢幂等键日志 = 重复下单风险。**
- 修复方向：生产代码路径禁止默认 `:memory:`，缺 env 即 fail-closed 拒绝启动。

### H5. CI 实际是红的
- `scripts/check_schemas.py` — 无 `encoding="utf-8"`：Windows 上读含中文注释的 envelope.json 抛 GBK UnicodeDecodeError 直接崩；Windows 路径分隔符 `\` 使所有 38+39 条 `schema_without_enum` 误报。Linux CI 上：`market/chief.briefing` 已被 `chief.py:298` 发射、有 schema 文件，但未登记进 `envelope.json` 的 enum（`emitted_not_in_enum` + `schema_without_enum`）→ **CI 的 schema 步骤在两端都红。**
- `.github/workflows/ci.yml:37 vs 49` — smoke job 的 `pip install $PKGS` 未装 pytest，随后第 49 行却执行 `pytest ...` → 该步骤必失败。
- 后果：CI 门禁实际不绿，却给开发者以「CI 在保护我」的安全错觉。

### H6. risk-policy Docker 镜像构建必失败
- `infra/containers/services/risk-policy.Dockerfile` 只 `COPY services/risk-policy`，而 `services/risk-policy/pyproject.toml:6` 依赖 `dsh-contracts` → `pip install` 尝试从 PyPI 拉取私有包，必失败。其余服务 Dockerfile 都正确复制了 domain-contracts。

---

## 3. 中危问题（Medium，纸面阶段也应修复）

- **M1. 客户端网络错误处理不一致** — `plugins/dsh-quant-gateway/client.py`：preview/approval/control 端点不包裹 `raise_unreachable`，网络抖动抛裸 `httpx` 异常，运行时把它当非网关错误直接 raise，**单 tick 全崩**。
- **M2. `decide_approval` TOCTOU** — `approval_store.py:98-113` 读-改-写跨事务，多 worker 下两个进程可同时通过 status 检查（进程内靠全局锁掩盖）。
- **M3. 幂等键 RESERVED 永不释放** — `orders.py:337-345` venue 提交未知异常后不标记 FAILED，无 TTL 回收机制，客户端不重试则密钥永久不可用。
- **M4. 对账静默回退错误账户** — `execution.py:662-665` account_id 找不到时用 `accounts[0]`，可能掩盖真实对账问题或误报。
- **M5. 审计直插私有成员** — `execution.py:260-269,489-512` 直用 `gateway._client` + `limit=1000` 全表扫审计，脆弱且随审计量增长退化。
- **M6. A 股 runner 不校验账户 market** — `scripts/run_a_stock_bot.py:49-62` 不像 crypto runner 校验市场匹配，币账户可被 A 股 bot 使用。
- **M7. 事件指纹与事故规则映射脆弱** — `plugins/dsh-market-chief/chief.py:41-49` 自由文本→规则 id 映射大量落空（"uncategorized"），去重目标落空；`BOT_HEALTH_WINDOW_EVENTS=200` 是事件数窗口而非时间窗。
- **M8. CSRF 密钥回退公开值** — `apps/dsh-bot-web/src/lib/write-guard.ts:7` 未设 `DSH_CSRF_SECRET` 时回退公开字符串 `"dev-csrf"`，令牌完整性依赖运气。
- **M9. 前端安全提示是假的** — `projection-api/main.py:23-26` 的 `_ACTION_RE` 关键词拦截可被自然语言绕过（"清仓""把仓平了"都不命中），却给用户"已被拦截"的错误安全感。
- **M10. 死代码与双份实现** — `plugins/dsh-trade-core/core.py`（41KB TradeExecutionCore 已分叉且无人引用，双份对账/双份轮询）；`ApprovalWorkflow.wait_for_decision` 无调用；插件版 `audit_order`/`/v1/audit-order` 无生产消费者。

---

## 4. 低危 / 质量项

- 重复 `_lock` 声明（gateway `storage.py:26,33`）；env 每次请求重解析（`auth.py`）。
- `check_schemas.py` 未声明 UTF-8 编码；`tzdata` 依赖未声明（Windows 上 `ZoneInfo("Asia/Shanghai")` 抛错，测试全量 collection error；CI 的 ubuntu 自带 tzdata 掩盖）。
- `run_crypto_bot.py:137-140` 路径归一化仅 POSIX；`run_a_stock_bot.py` 无归一化（不一致）。
- 幂等前缀硬编码 `"crypto-paper"`/`"ashare-paper"`，shadow 模式不反映真实模式。
- 事件重复发射（`core.py:355-363` 同一事件发两次）；`dsh-strategy-lab` 只读占位，profile 声明的工具全部未实现且被 CI 排除。

---

## 5. 做得好的地方（应保持）

- **幂等状态机**（RESERVED/SUBMITTED/COMPLETED/FAILED）+ 崩溃恢复路径设计扎实，测试覆盖好（并发、崩溃、fail-closed、重启恢复都有）。
- **fail-closed 一致性**：未注册市场 503、只读包装 403、risk-policy 不可达 503、auditor 不可达 503——全部有测试。
- **无 SQL 注入**（查询全部参数化）；**密钥处理良好**（无硬编码密钥，.env 系 gitignore）。
- **live 三层禁用**，实盘门槛诚实（README 自述 + 代码强制）。
- 分层与契约化清晰（event-schemas + domain-contracts + 客户端 SDK），单测 + 进程级冒烟 + 前端构建三层 CI 结构合理。
- 审批路径的「bot 提交 → 人工审批 → 再校验 → 二次硬风控 → 严格对账」闭环是真正加固的资金路径。

---

## 6. 优化路线图（按优先级）

### P0 — 上实盘前必须完成
1. 审批绑定订单 + 过期 + 一次性使用（C1）
2. 删除重复的 Risk Auditor 实现，统一 contract，CI 走真实 wiring 路径（C2）
3. 风控第二道防线改为读取权威账户/持仓状态推导；缺字段拒绝而非补 0；权益异常 fail-closed 拒绝而非 kill switch（C3）
4. 修复 A 股 paper 模式整手/T+1 跳过问题，接线 AStockMarketPolicy（C4）
5. 控制/取消/审计路径补齐审批与真实 outcome；引入独立 approve scope 做职责分离（C5/C6/H1）
6. 修复 check_schemas.py（UTF-8 + 路径分隔符 + 补 chief.briefing 枚举）与 CI smoke job 的 pytest 安装（H5）
7. 生产路径禁 `:memory:` 默认，缺 DB env 拒绝启动（H4）

### P1 — 生产前完成
8. 多 worker 订单号唯一化 + runtime 库 WAL/busy_timeout/乐观锁 + 测试覆盖订单号碰撞（H3）
9. 认证落地：evolution/risk-policy/risk-auditor/incident-center 全量鉴权（H1/H2）
10. 证据真实性验证（ref 对应真实实验/回测）+ 结论幂等键纳入 approval_id + 统一证据哈希算法（H2）
11. 修复 risk-policy Dockerfile；客户端网络错误统一包裹；幂等键 RESERVED 回收（H6/M1/M3）

### P2 — 工程质量
12. 清除死代码（dsh-trade-core 双份、wait_for_decision、audit_order、重复 _lock）
13. 声明 tzdata 依赖；Windows 路径归一化统一；事件指纹映射结构化
14. projection 关键词拦截改为真意图识别或移除假安全提示；CSRF 强制密钥

---

## 7. 本次审计的实测环境备注

- 运行测试需先 `pip install -e` 全部 16 个 pyproject 包（当前 venv 缺失 9 个——README 安装命令可一键完成）并补装 `tzdata`。
- Windows 下 `uvicorn --workers` 会泄漏子进程并占住端口：审计后已清理所有僵尸进程，并建议在测试 teardown 中按进程组（taskkill /T）或 `multiprocessing` 的主动清理方式处理。