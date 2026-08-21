from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from dsh_contracts.enums import (
    ApprovalStatus,
    ExecutionLane,
    IntelGrade,
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
    exported_at: datetime | None = None
    snapshot_age_seconds: int | None = None
    export_age_seconds: int | None = None
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
    evidence_refs: list[str] | None = None
    quantity: str | None = None
    entry_price: str | None = None
    source_action: str | None = None
    why_source: list[str] | None = None


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
    order_type: str | None = None
    limit_price: Decimal | None = None


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
    intent_digest: str | None = None
    expires_at: datetime | None = None
    consumed_key: str | None = None
    consumed_request_hash: str | None = None
    consumed_order_id: str | None = None
    consumed_at: datetime | None = None


class EntryPlan(_Contract):
    trigger_price: str | None = None
    conditions: list[str] = Field(default_factory=list)
    max_capital_ratio: str = "0.00"
    execute_by: str | None = None


class ExitPlan(_Contract):
    stop_loss: str | None = None
    take_profit: str | None = None
    time_exit: str | None = None
    invalidation: list[str] = Field(default_factory=list)


class DecisionRecord(_Contract):
    """决策账本：情报/信号 → 决策 → 订单/成交 → 审计。"""

    decision_id: str
    market: Market
    symbol: str | None = None
    intel_grade: IntelGrade
    execution_lane: ExecutionLane
    event_id: str | None = None
    intelligence_item_id: str | None = None
    signal_id: str | None = None
    strategy_id: str | None = None
    strategy_version: str | None = None
    risk_snapshot_id: str | None = None
    task_id: str | None = None
    order_id: str | None = None
    fill_id: str | None = None
    audit_id: str | None = None
    capital_budget: str = "0"
    max_risk: str = "0"
    requires_approval: bool = True
    can_apply: bool = False
    action: str
    direction: str | None = None
    confidence: str | None = None
    impact_horizon: str | None = None
    entry_plan: EntryPlan | None = None
    exit_plan: ExitPlan | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    # 链路延伸：持仓回合与优化候选（第一刀补齐）
    episode_id: str | None = None
    candidate_id: str | None = None


class MarketEvent(_Contract):
    """情报决策执行引擎的入口对象。

    回答：发生了什么？是否第一手？影响什么标的？市场是否已反映？
    每个判断必须有原文证据（evidence_refs），重复事件不重复决策。
    """

    event_id: str
    market: Market
    source_id: str
    source_url: str | None = None
    authority: str | None = None  # official | regulator | exchange | secondary | rumor
    first_hand: bool = False
    event_type: str
    symbol: str | None = None
    sector: str | None = None
    title: str
    summary: str | None = None
    published_at: datetime
    observed_at: datetime
    importance: float = 0.0
    direction: str | None = None  # POSITIVE | NEGATIVE | NEUTRAL
    horizon: str | None = None
    priced_in: bool | None = None  # 市场是否已反映；未知即 None，不得编造
    duplicate_of: str | None = None  # 去重后指向首个事件
    evidence_refs: list[str] = Field(default_factory=list)


class ExecutionPlan(_Contract):
    """决策的资金方案：进入、仓位、执行时机与退出，一并保存。

    回答：买/卖/不动？用多少资金？何时执行？什么条件退出？
    失效条件（invalidation）落在 exit_plan 中。
    """

    action: str  # BUY | SELL | REDUCE | EXIT | HOLD | NO_ACTION
    entry: EntryPlan
    exit: ExitPlan
    capital_budget: str = "0"
    max_risk: str = "0"
    execute_by: str | None = None
    opportunity_cost_if_skipped: str | None = None
    can_apply: bool = False  # Live 未开门禁前恒为 False


class PositionEpisode(_Contract):
    """一次进场到退出的完整回合，是 1h/1d/3d 结果跟踪的锚点。

    每笔成交必须挂到一个回合；退出后回填实际结果，
    情报决策由此知道自己最后赚没赚钱。
    """

    episode_id: str
    decision_id: str
    market: Market
    symbol: str
    side: str  # BUY | SELL
    entry_fill_id: str | None = None
    entry_price: str | None = None
    entry_at: datetime | None = None
    quantity: str = "0"
    exit_fill_id: str | None = None
    exit_price: str | None = None
    exit_at: datetime | None = None
    exit_reason: str | None = None
    realized_pnl: str | None = None
    fees: str = "0"
    status: str = "OPEN"  # OPEN | CLOSED
    outcomes: dict[str, str] = Field(default_factory=dict)  # 1h | 1d | 3d -> 观察值


class TradeAudit(_Contract):
    """每笔交易结束后的质量审计：回答触发、入场、仓位、执行与退出质量。

    维度评分必须来自可复算的事实字段，LLM 只做归因解释，
    不替代评分事实；can_apply 恒为 False，审计不直接改线上策略。
    """

    audit_id: str
    decision_id: str
    episode_id: str | None = None
    fill_id: str | None = None
    strategy_id: str | None = None
    strategy_version: str | None = None
    overall_score: int
    dimensions: dict[str, str] = Field(default_factory=dict)  # 维度 -> 评分说明(JSON)
    findings: list[str] = Field(default_factory=list)
    hold_hours: float | None = None
    realized_pnl: str | None = None
    can_apply: bool = False


class OptimizationCandidate(_Contract):
    """交易质量进化引擎的输出：带反事实数据的优化建议。

    actual/replayed/backtest 必须来自历史重放或样本外回测，
    不是凭空建议。晋级必须走 SHADOW -> PAPER -> 人工审批。
    """

    candidate_id: str
    market: Market
    title: str
    rule_id: str | None = None
    reason: str | None = None
    actual_pnl: str | None = None
    replayed_pnl: str | None = None
    delta_pnl: str | None = None
    backtest: dict[str, str] = Field(default_factory=dict)
    stage: str = "SUGGESTION"  # SUGGESTION | REPLAY | BACKTEST | SHADOW | PAPER
    next_stage: str | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    source_audit_ids: list[str] = Field(default_factory=list)
    can_apply: bool = False


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
