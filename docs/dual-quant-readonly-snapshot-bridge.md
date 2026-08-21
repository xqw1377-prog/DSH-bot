# 双量化只读快照桥

DSH Bot 当前是本机双市场 Paper/Shadow 只读控制台。它统一展示 6celue 与 ZISU 的账户、持仓、资金、数据新鲜度和运行状态，能够区分闭市与故障，并在数据异常时失败关闭。它不审批、不下单、不持有交易密钥，尚不是自动交易机器人。

这是**统一只读视图**，不是统一账本。6celue 与 ZISU 仍是各自账户与交易记录的权威来源；DSH 不拥有账本，也不替代两边的正式对账。

本桥**不修改** `6celue_v5` 与 `ZISU` 交易引擎，**不增加**任何下单能力，**不读取**币所或券商密钥。**不解除** `live` 门禁。

## 合并后控制台应呈现

| Bot | 运行模式 | 数据源 | 资金能力 |
| --- | --- | --- | --- |
| Market Chief | READ ONLY | 两市场投影汇总 | 无 |
| Crypto Bot | SHADOW | 6celue Demo 快照 | 无下单 |
| A 股 Bot | SHADOW | ZISU Paper 快照 | 无下单 |

顶部全局模式必须是 `SHADOW`，不能出现可操作的 `LIVE`。

三个 Bot 已接通 Shadow 决策闭环：消费量化系统正式信号，记录幂等 `SHADOW_RECORDED`，由 Chief 汇总每日作战简报，并跟踪建议价与后续价格。仍不审批、不下单。

| Bot | 现在能做 | 下一步 |
| --- | --- | --- |
| Market Chief | 汇总健康、事故，并生成每日作战简报 | 人工批准后的模拟下单编排 |
| Crypto Bot | 消费 `state_live/signals.jsonl` 正式信号，生成 Shadow 决策与复盘 | 向 6celue Demo 提交模拟单 |
| A 股 Bot | 消费 cockpit `policy_decisions` 正式信号，结合时段/整手/T+1 记录执行或放弃 | 向 ZISU Paper 提交模拟单 |

## 边界

| 允许 | 禁止 |
|---|---|
| 读 `state.json` / ZISU HTTP | 抓 Streamlit 8501 |
| 写 `CRYPTO.json` / `A_SHARE.json` | 直连 `zisu.db` |
| Bot `mode=shadow` | `live`、Paper 成交、审批写 |
| Gateway `READ_ONLY=1` | 即使持有 `write` scope，写接口仍 403 |

## 数据源（全部走环境变量）

| 市场 | 变量 | 说明 |
|---|---|---|
| CRYPTO | `DSH_CRYPTO_STATE_JSON` | 6celue `state_live/state.json` |
| CRYPTO | `DSH_CRYPTO_SIGNALS_JSONL` | 正式信号；默认 `state.json` 同目录 `signals.jsonl` |
| A_SHARE | `DSH_A_SHARE_WALLET_URL` | `GET /api/paper/wallet` |
| A_SHARE | `DSH_A_SHARE_SCREEN_URL` | `GET /api/trade/screen` |
| A_SHARE | `DSH_A_SHARE_COCKPIT_URL` | 正式决策；默认由 wallet 推导 `/api/cockpit` |
| 输出 | `QUANT_GATEWAY_SNAPSHOT_DIR` | 独立目录，Gateway 只读 |

账户固定：`paper-crypto-001` / `paper-a-share-001`。

`/api/trade/screen` 只导出为 `screen_results[].kind=SCREEN_RESULT`，**不会**进入 `signals`，也不能据此推断交易意图。

## Shadow 决策闭环

**仍不开放交易。** 信号必须由原量化系统明确输出；DSH 只记录、解释和评估，不能从 `screen_results`、`daily_universe` 或 `pending_suggestions` 自行推断。

```text
量化系统正式信号
→ 专业 Bot 判断
→ 风险规则检查
→ 生成 SHADOW_RECORDED
→ Chief 汇总排序 / 每日作战简报
→ 跟踪后续行情
→ 复盘（建议价 vs 后续价）
```

| 市场 | 正式信号源 | 纳入 | 排除 |
| --- | --- | --- | --- |
| CRYPTO | `signals.jsonl`（`SignalRecord`） | `action∈{pending,executed}` 且 `side∈{LONG,SHORT}` | `filtered` / `rejected` / `cooldown`；`V5_SYNC_ADOPT` / `EVENT_EXECUTOR` |
| A_SHARE | `GET /api/cockpit` → `policy_decisions` | `executable=true` 且 `action∈{buy,add,reduce,exit}` | `/api/trade/screen`、`screen_results` |

信号文件或 cockpit 失败时，`signals=[]`，账户快照仍可成功。`valid_until` 为派生字段（Crypto 默认 +15min，A 股默认 +30min）。

每条 Shadow 决策包含：买入/卖出/持有/放弃、数量与价格区间、策略版本与强度、预计仓位/损失上限/主要风险、为什么做/为什么不做、有效期与证据、后续走势与模拟收益，并标注「仅模拟，不会下单」。

控制台：`/shadow` 看决策与简报；投影 `GET /v1/shadow-decisions`、`GET /v1/chief/briefing`。

### 验收

1. Crypto 和 A 股各接入至少一条真实正式信号。
2. 每条信号只产生一条幂等的 `SHADOW_RECORDED`。
3. 数据过期、闭市或风险超限时明确输出「不执行」及原因。
4. Chief 能汇总、排序并形成每日作战简报。
5. 连续运行后能比较「建议时价格」和「后续价格」，形成复盘数据。
6. 审批、订单、撤单和控制写入仍全部为零。

下一阶段才是经人工批准，只向 6celue Demo 和 ZISU Paper 提交模拟订单。`live` 仍然关闭。

## 失败关闭

- 不编造账户、持仓或价格
- 保留上次成功快照（last-good 视图）；`source_observed_at` **不更新**
- `data_fresh=false`、`degraded=true`，控制台 Data 维为 `STALE`
- A 股闭市：`MARKET_CLOSED`，不记数据事故；接口异常才标数据故障
- 快照递归拒绝 API Key / Secret / Token / Cookie / 数据库连接串

## 运行

```powershell
copy .env.shadow.example .env.shadow
# 填 DSH_CRYPTO_STATE_JSON 与 ZISU URL
.\scripts\start-shadow-bridge.ps1
```

```powershell
python scripts/run_crypto_bot.py --mode shadow
python scripts/run_a_stock_bot.py --mode shadow
```

## 验收命令（CI 必须原样执行）

```bash
pytest packages/dsh-snapshot-bridge packages/dsh-runtime/tests/test_market_closed.py packages/dsh-runtime/tests/test_shadow_loop.py services/quant-gateway/tests/test_snapshot_adapter.py services/quant-gateway/tests/test_snapshot_readonly_gate.py services/projection-api/tests/test_projection.py -q
bash scripts/smoke_shadow_snapshot.sh
```

冒烟覆盖：`write` scope 仍 403；两 Bot 连续 3 tick 审批/订单/撤单均为 0；`live` 启动失败。
