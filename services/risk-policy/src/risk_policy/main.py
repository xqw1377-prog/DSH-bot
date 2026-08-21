"""全局风险预算与订单风控检查。

职责：维护每个市场的风险预算（最大持仓、最大回撤、单笔最大损失占比），
并对外提供订单风控检查接口。本服务是「预算权威」，Quant Gateway 在提交
订单前调用 /v1/check-order 做二次硬风控；服务不可达时 Gateway 失败关闭。

预算可用环境变量覆盖（RISK_BUDGET_A_SHARE_MAX_POSITION 等），
生产应替换为持久化存储与审批变更流程。
"""

import os
from decimal import Decimal, InvalidOperation

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from dsh_contracts import Market

from risk_policy.service_auth import require_service_key

app = FastAPI(
    title="Risk Policy",
    description="全局风险预算、限制命中与二次风控策略。",
    version="0.2.0",
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
    # CRITICAL 才允许触发 Kill Switch；HIGH 只拒单
    severity: str = "OK"
    kill_switch: bool = False


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "service": "risk-policy"}


@app.get("/v1/risk-budget", dependencies=[Depends(require_service_key)])
def get_risk_budget(market: Market | None = None) -> list[dict]:
    budgets = _budgets.values()
    if market is not None:
        budgets = (b for b in budgets if b.market == market)
    return [b.model_dump(mode="json") for b in budgets]


@app.get("/v1/risk-budget/{market}", dependencies=[Depends(require_service_key)])
def get_market_budget(market: Market) -> dict:
    budget = _budgets.get(market)
    if budget is None:
        raise HTTPException(status_code=404, detail=f"no budget configured for {market}")
    return budget.model_dump(mode="json")


@app.post("/v1/check-order", dependencies=[Depends(require_service_key)])
def check_order(check: OrderRiskCheck) -> dict:
    budget = _budgets.get(check.market)
    if budget is None:
        # 未配置预算 = 失败关闭
        return CheckResult(
            passed=False,
            limits_hit=[f"no_budget:{check.market}"],
            severity="CRITICAL",
            kill_switch=True,
        ).model_dump(mode="json")

    limits: list[str] = []
    critical = False
    data_unavailable = False
    if check.notional > budget.max_position:
        limits.append(
            f"max_position: notional {check.notional} > {budget.max_position}"
        )
    if check.equity <= 0:
        # 数据不可用 ≠ 真实风险事件：只拒当单，绝不触发 Kill Switch
        limits.append("equity_unavailable")
        data_unavailable = True
    elif check.worst_case_loss > check.equity * budget.max_loss_ratio_per_order:
        limits.append(
            "max_loss_ratio_per_order: "
            f"{check.worst_case_loss} > equity*{budget.max_loss_ratio_per_order}"
        )
        critical = True
    if check.quantity <= 0:
        limits.append("non_positive_quantity")

    if not limits:
        severity = "OK"
    elif data_unavailable and not critical:
        severity = "DATA_UNAVAILABLE"
    elif critical:
        severity = "CRITICAL"
    else:
        severity = "HIGH"
    result = CheckResult(
        passed=not limits,
        limits_hit=limits,
        budget=budget,
        severity=severity,
        kill_switch=critical,
    )
    return result.model_dump(mode="json")

# Prometheus 指标：infra/observability/prometheus.yml 抓取 /metrics
from prometheus_client import make_asgi_app  # noqa: E402

app.mount("/metrics", make_asgi_app())
