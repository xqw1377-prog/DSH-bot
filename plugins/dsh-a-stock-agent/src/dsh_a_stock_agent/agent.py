"""A 股 Bot：与 Crypto 共用 TradeExecutionCore，Paper 先、LLM 不进。"""

from dsh_contracts import Market
from dsh_runtime.execution import TradeExecutionCore


class AShareAgent(TradeExecutionCore):
    name = "a-stock-bot"

    def __init__(
        self,
        gateway,
        approvals,
        account_id: str,
        min_strength: float = 0.6,
        mode: str = "paper",
    ):
        super().__init__(
            name="a-stock-bot",
            market=Market.A_SHARE,
            gateway=gateway,
            approvals=approvals,
            account_id=account_id,
            min_strength=min_strength,
            mode=mode,
            idempotency_prefix="ashare-paper",
        )
