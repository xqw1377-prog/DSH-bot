"""全局风险预算与订单风控检查。

职责：维护每个市场的风险预算（最大持仓、最大回撤、单笔最大损失占比），
并对外提供订单风控检查接口。本服务是「预算权威」，Quant Gateway 在提交
订单前调用 /v1/check-order 做二次硬风控；服务不可达时 Gateway 失败关闭。

风控规则触发（PRD 设计红线）：
- Kill Switch 只能由 risk-policy 签发的结构化 CRITICAL 事件触发，
  LLM/Agent 文本判断只能产生告警，不能自动停盘。
- POST /v1/rule-violations 由风控监控器或 Gateway 上报规则违反，
  服务签发 source="risk-policy" 的事件供 Incident Center 拉取。
- GET  /v1/rule-violations 供 Incident Center 拉取未确认的 CRITICAL 事件。
- DELETE /v1/rule-violations/{id} 由 Incident Center 在执行 Kill Switch 后确认。

预算可用环境变量覆盖（RISK_BUDGET_A_SHARE_MAX_POSITION 等），
生产应替换为持久化存储与审批变更流程。
"""

import os
import threading
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from dsh_contracts import Market

app = FastAPI(
    title="Risk Policy",
    description="全局风险预算、限制命中与二次风控策略、Kill Switch 签发权威。",
    version="0.3.0",
)


class RiskBudget(BaseModel):
    market: Market
    max_position: Decimal
    max_drawdown: Decimal
    # 单笔订单最坏损失占权益的最大比例
    max_loss_ratio_per_order: Decimal = Decimal("0.01")


_DEFAULT_BUDGETS: dict[Market, RiskBudget] = {
    Market.A_SHARE: RiskBudget(
        market=Market.A_SHARE,
        max_position=Decimal("100000"),
        max_drawdown=Decimal("0.05"),
        max_loss_ratio_per_order=Decimal("0.01"),
    ),
    Market.CRYPTO: RiskBudget(
        market=Market.CRYPTO,
        max_position=Decimal("50000"),
        max_drawdown=Decimal("0.10"),
        max_loss_ratio_per_order=Decimal("0.02"),
    ),
}


def _load_budgets() -> dict[Market, RiskBudget]:
    """从环境变量覆盖默认预算，格式错误时失败关闭（拒绝启动配置）。"""
    budgets = {m: b.model_copy(deep=True) for m, b in _DEFAULT_BUDGETS.items()}
    for market in Market:
        prefix = f"RISK_BUDGET_{market.value}"
        for field in ("MAX_POSITION", "MAX_DRAWDOWN", "MAX_LOSS_RATIO_PER_ORDER"):
            raw = os.environ.get(f"{prefix}_{field}")
            if raw is None:
                continue
            try:
                value = Decimal(raw)
            except InvalidOperation as exc:
                raise RuntimeError(f"invalid env {prefix}_{field}={raw!r}") from exc
            setattr(budgets[market], field.lower(), value)
    return budgets


_budgets = _load_budgets()


class OrderRiskCheck(BaseModel):
    """订单风控检查输入，字段来自 OrderIntent 与 RiskSnapshot。"""

    market: Market
    account_id: str
    symbol: str
    quantity: Decimal
    # 订单名义价值（由 Gateway/适配器估算）
    notional: Decimal
    # 风险快照给出的最坏损失
    worst_case_loss: Decimal
    # 账户权益（缺失时无法检查损失比例，失败关闭）
    equity: Decimal


class CheckResult(BaseModel):
    passed: bool
    limits_hit: list[str] = Field(default_factory=list)
    budget: RiskBudget | None = None


# ---- 风控规则触发（Kill Switch 唯一可信源）----

class RuleViolation(BaseModel):
    """风控规则违反事件，由 risk-policy 签发。

    source 固定为 "risk-policy"，Incident Center 只接受此来源的 CRITICAL
    事件触发 Kill Switch，LLM/Agent 文本判断不能自动停盘。
    """
    severity: str = Field(..., pattern="^(HIGH|CRITICAL)$")
    rule_id: str
    market: Market
    measured: float
    limit: float
    account_id: str | None = None
    evidence_refs: list[str] = Field(default_factory=list)


class RuleViolationRecord(RuleViolation):
    violation_id: str
    occurred_at: str
    acknowledged: bool = False
    source: str = "risk-policy"


# 进程内存储（生产应替换为持久化存储 + WAL）
_violations: dict[str, RuleViolationRecord] = {}
_violations_lock = threading.Lock()


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "service": "risk-policy"}


@app.get("/v1/risk-budget")
def get_risk_budget(market: Market | None = None) -> list[dict]:
    budgets = _budgets.values()
    if market is not None:
        budgets = (b for b in budgets if b.market == market)
    return [b.model_dump(mode="json") for b in budgets]


@app.get("/v1/risk-budget/{market}")
def get_market_budget(market: Market) -> dict:
    budget = _budgets.get(market)
    if budget is None:
        raise HTTPException(status_code=404, detail=f"no budget configured for {market}")
    return budget.model_dump(mode="json")


@app.post("/v1/check-order")
def check_order(check: OrderRiskCheck) -> dict:
    budget = _budgets.get(check.market)
    if budget is None:
        # 未配置预算 = 失败关闭
        return CheckResult(
            passed=False, limits_hit=[f"no_budget:{check.market}"]
        ).model_dump(mode="json")

    limits: list[str] = []
    if check.notional > budget.max_position:
        limits.append(
            f"max_position: notional {check.notional} > {budget.max_position}"
        )
    if check.equity <= 0:
        limits.append("equity_unavailable")
    elif check.worst_case_loss > check.equity * budget.max_loss_ratio_per_order:
        limits.append(
            "max_loss_ratio_per_order: "
            f"{check.worst_case_loss} > equity*{budget.max_loss_ratio_per_order}"
        )
    if check.quantity <= 0:
        limits.append("non_positive_quantity")

    result = CheckResult(passed=not limits, limits_hit=limits, budget=budget)
    return result.model_dump(mode="json")


@app.post("/v1/rule-violations", response_model=RuleViolationRecord)
def report_rule_violation(violation: RuleViolation) -> dict:
    """风控规则违反上报：签发结构化事件供 Incident Center 拉取。

    只有 risk-policy 签发的事件才能触发 Kill Switch（Incident Center 校验
    source 字段）。这是 Kill Switch 安全设计的核心：避免 LLM/文本判断
    自动停盘。
    """
    record = RuleViolationRecord(
        **violation.model_dump(),
        violation_id=f"rv-{uuid4().hex[:12]}",
        occurred_at=datetime.now(UTC).isoformat(),
        acknowledged=False,
        source="risk-policy",
    )
    with _violations_lock:
        _violations[record.violation_id] = record
    return record.model_dump(mode="json")


@app.get("/v1/rule-violations", response_model=list[RuleViolationRecord])
def list_rule_violations(
    market: Market | None = None,
    severity: str | None = None,
    acknowledged: bool | None = None,
) -> list[dict]:
    """供 Incident Center 拉取未确认的 CRITICAL 事件。

    默认只返回未确认的 CRITICAL（Kill Switch 触发源）。
    """
    with _violations_lock:
        records = list(_violations.values())
    result = []
    for r in records:
        if market is not None and r.market != market:
            continue
        if severity is not None and r.severity != severity:
            continue
        if acknowledged is not None and r.acknowledged != acknowledged:
            continue
        result.append(r.model_dump(mode="json"))
    return result


@app.delete("/v1/rule-violations/{violation_id}")
def acknowledge_rule_violation(violation_id: str) -> dict:
    """Incident Center 在执行 Kill Switch 后确认该事件，避免重复触发。"""
    with _violations_lock:
        record = _violations.get(violation_id)
        if record is None:
            raise HTTPException(404, detail=f"violation {violation_id} not found")
        record.acknowledged = True
    return {"violation_id": violation_id, "acknowledged": True}
