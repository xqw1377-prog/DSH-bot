"""Crypto Bot：共享执行核的数字资产薄封装。"""

from dsh_contracts import Market
from dsh_runtime.execution import TradeExecutionCore


class CryptoAgent(TradeExecutionCore):
    name = "crypto-bot"

    def __init__(
        self,
        gateway,
        approvals,
        account_id: str,
        min_strength: float = 0.6,
        mode: str = "paper",
    ):
        super().__init__(
            name="crypto-bot",
            market=Market.CRYPTO,
            gateway=gateway,
            approvals=approvals,
            account_id=account_id,
            min_strength=min_strength,
            mode=mode,
            idempotency_prefix="crypto-paper",
        )
