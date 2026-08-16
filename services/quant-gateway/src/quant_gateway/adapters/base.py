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

    @abstractmethod
    def cancel_order(self, order_id: str) -> dict: ...

    @abstractmethod
    def pause_strategy(self, strategy_id: str) -> None: ...

    @abstractmethod
    def resume_strategy(self, strategy_id: str) -> None: ...

    @abstractmethod
    def emergency_stop(self, account_id: str | None = None) -> None: ...
