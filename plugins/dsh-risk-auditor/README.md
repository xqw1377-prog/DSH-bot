# dsh-risk-auditor

独立风控验证边界：对订单风控结论与策略晋级做二次校验。

## 库 API

- `RiskAuditor.evaluate_order` / `audit_order`
- `RiskAuditor.evaluate_promotion` / `audit_promotion`

## HTTP 服务（独立进程）

```bash
RISK_AUDITOR_DB=.data/risk-auditor.db \
  uvicorn dsh_risk_auditor.service:app --port 8005
```

| 路径 | 说明 |
|---|---|
| `GET /health` / `GET /healthz` | 健康检查 |
| `POST /v1/audit-promotion` | 晋级审计，返回 `audit_id`、`strategy_version`、`evidence_hash`、`approved`、`reason` |
| `GET /v1/audits/{audit_id}` | 按 ID 读取已持久化的审计结果 |

strategy-evolution 通过 `STRATEGY_EVOLUTION_AUDITOR_URL` 调用本服务；
不可达或拒绝时晋级失败关闭。禁止在 evolution 进程内直接 import 本库做门禁。
