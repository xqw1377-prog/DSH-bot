from enum import StrEnum


class Market(StrEnum):
    A_SHARE = "A_SHARE"
    CRYPTO = "CRYPTO"


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(StrEnum):
    """订单执行状态机（venue 视角），见 PRD 附录 A.1。

    仅描述订单在交易所/纸面系统的执行生命周期，与账户对账状态
    (ReconciliationStatus) 正交分离：一个 FILLED 订单的对账状态
    可以是 PENDING / IN_PROGRESS / MATCHED / MISMATCH / RECONCILED。
    """

    INTENT_CREATED = "INTENT_CREATED"
    RISK_PASSED = "RISK_PASSED"
    APPROVAL_PENDING = "APPROVAL_PENDING"
    APPROVED = "APPROVED"
    SUBMITTED = "SUBMITTED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"
    FAILED = "FAILED"


class ReconciliationStatus(StrEnum):
    """账户对账状态机（与 OrderStatus 正交分离，见 PRD 11.4）。

    描述订单成交后对持仓/资金影响的对账进度，独立于订单执行状态：
    - PENDING: 订单未成交或尚未开始对账
    - IN_PROGRESS: 正在查询持仓/资金进行比对
    - MATCHED: 持仓/资金与预期一致
    - MISMATCH: 持仓/资金与预期不符（失败关闭）
    - RECONCILED: 对账完成，任务可关闭
    """

    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    MATCHED = "MATCHED"
    MISMATCH = "MISMATCH"
    RECONCILED = "RECONCILED"


class StrategyStage(StrEnum):
    """策略进化状态机，见 PRD 10.4。"""

    DRAFT = "DRAFT"
    BACKTESTED = "BACKTESTED"
    VALIDATED = "VALIDATED"
    PAPER = "PAPER"
    SHADOW = "SHADOW"
    APPROVED = "APPROVED"
    CANARY = "CANARY"
    PRODUCTION = "PRODUCTION"
    RETIRED = "RETIRED"
    ROLLED_BACK = "ROLLED_BACK"


class ApprovalStatus(StrEnum):
    REQUESTED = "REQUESTED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class IncidentStatus(StrEnum):
    """事故状态机，见 PRD 附录 A.2。"""

    OPEN = "OPEN"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    MITIGATING = "MITIGATING"
    MITIGATED = "MITIGATED"
    RECOVERING = "RECOVERING"
    RESOLVED = "RESOLVED"
    REVIEWED = "REVIEWED"


class TaskStatus(StrEnum):
    """Bot 任务状态机，见 PRD 附录 A.3。"""

    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    WAITING_FOR_TOOL = "WAITING_FOR_TOOL"
    WAITING_FOR_APPROVAL = "WAITING_FOR_APPROVAL"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
