from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from dsh_contracts.enums import (
    ApprovalStatus,
    Market,
    OrderSide,
    StrategyStage,
    TaskStatus,
)


class _Contract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class HealthStatus(_Contract):
    """get_health 响应：系统、数据源、交易通道和时钟状态。"""

    market: Market
    system_ok: bool
    data_fresh: bool
    trading_channel_ok: bool
    clock_skew_ms: int
    degraded: bool = False
    detail: str | None = None
    as_of: datetime
    source_system: str | None = None
    source_mode: str | None = None
    source_observed_at: datetime | None = None
    snapshot_id: str | None = None
    market_session: str | None = None


class Position(_Contract):
    market: Market
    account_id: str
    symbol: str
    quantity: Decimal
    available_quantity: Decimal
    frozen_quantity: Decimal = Decimal("0")
    avg_cost: Decimal
    currency: str
    as_of: datetime


class AccountSummary(_Contract):
    market: Market
    account_id: str
    cash: Decimal
    equity: Decimal
    margin_used: Decimal | None = None
    available_cash: Decimal | None = None
    frozen_cash: Decimal | None = None
    currency: str
    reconciliation_version: str
    as_of: datetime


class Signal(_Contract):
    signal_id: str
    market: Market
    strategy_id: str
    strategy_version: str
    symbol: str
    side: OrderSide
    strength: float | None = None
    generated_at: datetime
    valid_until: datetime
    data_snapshot_id: str


class RiskSnapshot(_Contract):
    risk_snapshot_id: str
    market: Market
    account_id: str
    position_before: Decimal
    position_after: Decimal
    risk_budget_delta: Decimal
    worst_case_loss: Decimal
    limits_hit: list[str] = Field(default_factory=list)
    as_of: datetime


class OrderIntent(_Contract):
    """订单意图，见 PRD 11.2。所有数字使用明确精度，不使用模糊数量。"""

    idempotency_key: str
    market: Market
    account_id: str
    strategy_id: str
    strategy_version: str
    symbol: str
    side: OrderSide
    quantity: Decimal
    valid_until: datetime
    signal_snapshot_id: str
    risk_snapshot_id: str
    approval_id: str | None = None


class OrderPreview(_Contract):
    intent: OrderIntent
    estimated_cost: Decimal
    estimated_slippage: Decimal
    risk: RiskSnapshot


class Approval(_Contract):
    approval_id: str
    status: ApprovalStatus
    market: Market
    requested_by_bot: str
    requested_at: datetime
    decided_by: str | None = None
    decided_at: datetime | None = None
    subject_type: str  # order | strategy_promotion | risk_budget | control_action
    subject_id: str
    evidence_refs: list[str] = Field(default_factory=list)


class Experiment(_Contract):
    """研究实验账本条目，见 PRD 10.4。实验环境无生产密钥。"""

    experiment_id: str
    market: Market
    strategy_id: str
    hypothesis: str
    data_snapshot_id: str
    status: TaskStatus = TaskStatus.QUEUED
    created_by_bot: str
    created_at: datetime
    result_ref: str | None = None


class StrategyCandidate(_Contract):
    """策略晋级状态机条目，见 PRD 附录 A.1。

    单次回测不足以晋级：stage 推进必须附带证据引用。
    """

    candidate_id: str
    market: Market
    strategy_id: str
    strategy_version: str
    stage: StrategyStage = StrategyStage.DRAFT
    experiment_id: str | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    approval_id: str | None = None
    updated_at: datetime
