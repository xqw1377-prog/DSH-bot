# DSH Bot

基于 DeepSeek Harness 的持续进化量化 Agent 平台。

DSH Bot 是量化系统上方的智能控制面：让用户以自然语言管理研究、信号、策略、风险、审批、异常和策略进化，同时保留确定性量化系统对资金和订单的最终控制。

## 架构原则

- 一个产品入口，一个 Market Chief；A 股和数字资产分别由专业 Bot 管理
- DSH 负责理解、协调、记忆、调度、研究、审批和复盘；量化系统负责计算、硬风控、账本和执行
- 任何 Bot 都不能绕过 Quant Gateway，也不能直接读取交易密钥
- 持续优化必须通过研究、验证、Paper/Shadow、审批、Canary 和回滚门禁

## 仓库结构

```
apps/
  dsh-bot-web/          # 产品前端（Next.js）：Bot Home、Chief Chat、审批、Portfolio、Strategy Lab
profiles/
  market-chief/         # 总控 Bot Profile
  a-stock-bot/          # A 股专业 Bot Profile
  crypto-bot/           # 数字资产专业 Bot Profile
  strategy-lab/         # 策略实验室 Profile（隔离环境，无生产密钥）
plugins/
  dsh-market-chief/     # 总控插件
  dsh-a-stock-agent/    # A 股 Agent 插件
  dsh-crypto-agent/     # 币 Agent 插件
  dsh-strategy-lab/     # 实验室插件
  dsh-risk-auditor/     # 独立风控验证插件
  dsh-quant-gateway/    # Gateway 客户端插件
  dsh-trade-approval/   # 交易审批插件
  dsh-incident-center/  # 事故中心插件
services/
  quant-gateway/        # 统一协议、身份、授权、幂等、二次硬风控（FastAPI）
  strategy-evolution/   # 实验账本、验证门禁、策略晋级状态机（FastAPI）
  risk-policy/          # 全局风险预算与策略（FastAPI）
  projection-api/       # 面向前端的只读投影（FastAPI）
packages/
  domain-contracts/     # 领域对象 Pydantic 模型（订单意图、信号、审批等）
  event-schemas/        # 领域事件 JSON Schema（语言中立）
  client-sdk/           # 前端 TypeScript SDK
infra/
  containers/           # Dockerfile 与 compose
  observability/        # 监控与告警配置
  deployment/           # 部署清单
docs/                   # 产品与架构文档
```

## 快速开始

### 后端（Python 3.12+）

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e packages/domain-contracts -e services/quant-gateway -e services/strategy-evolution -e services/risk-policy -e services/projection-api
uvicorn quant_gateway.main:app --port 8001
```

### 前端（Node 20+ / pnpm）

```powershell
pnpm install
pnpm --filter dsh-bot-web dev
```

## 设计红线

- 实盘策略不能在运行中被 Bot 原地修改
- 交易密钥不能进入模型上下文
- DSH Session 不能成为唯一交易账本
- 策略晋级不能只依据单次回测
- 币系统故障不能影响 A 股系统，反之亦然
