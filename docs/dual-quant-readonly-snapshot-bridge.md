# 双量化只读快照桥

把两个**正在跑模拟盘**的量化系统，变成 DSH 的只读事实源。

本桥**不修改** `6celue_v5` 与 `ZISU` 交易引擎，**不增加**任何下单能力，**不读取**币所或券商密钥。

合并含义：两套量化系统已可接入 DSH 做**只读观察与决策验证**。
**不代表**实盘适配器完成，也**不解除** `live` 门禁。

## 合并后控制台应呈现

| Bot | 运行模式 | 数据源 | 资金能力 |
| --- | --- | --- | --- |
| Market Chief | READ ONLY | 两市场投影汇总 | 无 |
| Crypto Bot | SHADOW | 6celue Demo 快照 | 无下单 |
| A 股 Bot | SHADOW | ZISU Paper 快照 | 无下单 |

顶部全局模式必须是 `SHADOW`，不能出现可操作的 `LIVE`。

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
| A_SHARE | `DSH_A_SHARE_WALLET_URL` | `GET /api/paper/wallet` |
| A_SHARE | `DSH_A_SHARE_SCREEN_URL` | `GET /api/trade/screen` |
| 输出 | `QUANT_GATEWAY_SNAPSHOT_DIR` | 独立目录，Gateway 只读 |

账户固定：`paper-crypto-001` / `paper-a-share-001`。

`/api/trade/screen` 只导出为 `screen_results[].kind=SCREEN_RESULT`，**不会**进入 `signals`。

## 失败关闭

- 不编造账户、持仓或价格
- 保留上次成功账本；`source_observed_at` **不更新**
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
pytest packages/dsh-snapshot-bridge services/quant-gateway/tests/test_snapshot_adapter.py services/quant-gateway/tests/test_snapshot_readonly_gate.py services/projection-api/tests/test_projection.py packages/dsh-runtime/tests/test_market_closed.py -q
bash scripts/smoke_shadow_snapshot.sh
```

冒烟覆盖：`write` scope 仍 403；两 Bot 连续 3 tick 审批/订单/撤单均为 0；`live` 启动失败。
