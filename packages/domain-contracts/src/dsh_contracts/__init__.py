"""DSH Bot 领域对象契约。

权威数据归属见 PRD 第 12 章：DSH 仅保存投影与审计映射，
订单与成交的权威在现有量化/执行系统。
"""

from dsh_contracts.enums import (
    ApprovalStatus,
    IncidentStatus,
    Market,
    OrderSide,
    OrderStatus,
    ReconciliationStatus,
    StrategyStage,
    TaskStatus,
)
from dsh_contracts.models import (
    AccountSummary,
    Approval,
    Experiment,
    HealthStatus,
    OrderIntent,
    OrderPreview,
    Position,
    RiskSnapshot,
    Signal,
    StrategyCandidate,
)

__all__ = [
    "AccountSummary",
    "Approval",
    "ApprovalStatus",
    "Experiment",
    "HealthStatus",
    "IncidentStatus",
    "Market",
    "OrderIntent",
    "OrderPreview",
    "OrderSide",
    "OrderStatus",
    "Position",
    "ReconciliationStatus",
    "RiskSnapshot",
    "Signal",
    "StrategyCandidate",
    "StrategyStage",
    "TaskStatus",
]
