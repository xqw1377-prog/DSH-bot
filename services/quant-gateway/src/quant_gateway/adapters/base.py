"""市场适配器接口。

每个市场（A_SHARE / CRYPTO）由独立适配器接入现有量化系统，
账户、凭据、执行链和故障域严格分离（PRD 1.2）。
适配器实现必须满足：幂等、状态未知时查询而非盲目重试（PRD 11.3）。
"""

from abc import ABC, abstractmethod

from dsh_contracts import (
    AccountSummary,
    HealthStatus,
    OrderIntent,
    OrderPreview,
    Position,
    Signal,
)


class MarketAdapter(ABC):
    # 订单查询一致性声明，决定 SUBMISSION_UNKNOWN 恢复策略：
    # - STRONG：支持稳定 client_order_id 查询，且「查无」可强一致地判定
    #   为「从未接受」→ 允许确认不存在后自动释放幂等键重试
    # - EVENTUAL：查询最终一致，「查无」不能断定未接单 → 保持
    #   SUBMISSION_UNKNOWN，转人工事故
    # - UNSUPPORTED：不支持按幂等键查询 → 同 EVENTUAL
    order_lookup_consistency: str = "UNSUPPORTED"

    @abstractmethod
    def get_health(self) -> HealthStatus: ...

    @abstractmethod
    def get_positions(self, account_id: str | None = None) -> list[Position]: ...

    @abstractmethod
    def get_account_summary(self) -> list[AccountSummary]: ...

    @abstractmethod
    def get_signals(self) -> list[Signal]: ...

    @abstractmethod
    def preview_order(self, intent: OrderIntent) -> OrderPreview: ...

    @abstractmethod
    def request_order(self, intent: OrderIntent) -> str:
        """提交订单意图，返回权威订单 ID。必须使用 idempotency_key 去重。"""

    @abstractmethod
    def get_order_status(self, order_id: str) -> dict: ...

    def find_order_by_idempotency_key(self, key: str) -> dict | None:
        """按幂等键查询交易系统是否已接受该订单（SUBMISSION_UNKNOWN 恢复）。

        返回订单记录表示 venue 已接单（认领）；返回 None 必须表示
        「确定从未接受」——不确定时实现应抛异常（失败关闭）。
        """
        return None

    @abstractmethod
    def cancel_order(self, order_id: str) -> dict: ...

    @abstractmethod
    def pause_strategy(self, strategy_id: str) -> None: ...

    @abstractmethod
    def resume_strategy(self, strategy_id: str) -> None: ...

    @abstractmethod
    def emergency_stop(self, account_id: str | None = None) -> None: ...
