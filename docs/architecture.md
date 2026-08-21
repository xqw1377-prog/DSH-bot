# DSH Bot 架构说明

> 依据《DSH Bot 产品开发文档 v0.1》第 5、11-13 章整理。

## 分层

| 层级 | 职责 | 权威数据 |
|---|---|---|
| 产品体验层 (`apps/dsh-bot-web`) | 聊天、Bot 工作区、审批、报告、实验、事故中心 | 视图与投影，不作为资金权威 |
| DSH Runtime (`profiles/`, `plugins/`) | Session、Jobs、Workflow、Schedule、Subagent、Skills | Agent 事件、任务和审批记录 |
| 专业 Bot 层 | 总控、A 股、币、Strategy Lab、Risk Auditor | 决策建议和解释 |
| Quant Gateway (`services/quant-gateway`) | 统一协议、身份、授权、幂等、二次硬风控 | 请求与响应审计 |
| 量化系统层（外部） | 行情、策略、账本、订单、成交、策略级风控 | 账户、订单、成交和策略运行权威 |

## Quant Gateway 最小接口

只读：`get_health`、`get_positions`、`get_account_summary`、`get_signals`、`preview_order`、`get_order_status`

可改变资金状态（需更强授权 + 审批）：`request_order`、`cancel_order`、`pause/resume_strategy`、`emergency_stop`

## 策略状态机

```
DRAFT -> BACKTESTED -> VALIDATED -> PAPER -> SHADOW
      -> APPROVED -> CANARY -> PRODUCTION
      -> RETIRED | ROLLED_BACK
```

## 订单状态机

```
INTENT_CREATED -> RISK_PASSED -> APPROVAL_PENDING
               -> APPROVED -> SUBMITTED -> ACKNOWLEDGED
               -> PARTIALLY_FILLED -> FILLED
               -> CANCELLED | REJECTED | UNKNOWN
UNKNOWN -> RECONCILING -> ACKNOWLEDGED | FILLED | FAILED
```

## 隔离与安全边界

- A 股与币的账户、凭据、执行链、故障域严格分离
- Strategy Lab 使用隔离计算环境，禁止访问生产交易密钥
- DSH 到 Quant Gateway 仅通过稳定 REST/gRPC，不直接连接量化数据库
- 失败关闭：数据、对账、授权或风控不可用时默认拒绝交易
- Kill Switch 独立，不依赖 LLM 或单一 DSH 进程

## 服务端口约定（本地开发）

| 服务 | 端口 |
|---|---|
| quant-gateway | 8001 |
| strategy-evolution | 8002 |
| risk-policy | 8003 |
| projection-api | 8004 |
| intelligence-ingest（可选） | 8006 |
| dsh-bot-web | 3000 |
