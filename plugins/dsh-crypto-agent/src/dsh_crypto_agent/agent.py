"""Crypto Bot：共享执行核的数字资产薄封装。"""

from dsh_runtime import BotIntelligenceJob, StrategyAuditorJob
from dsh_runtime.intelligence import SIXCELUE_CRYPTO_UNIVERSE
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
        self._intelligence = BotIntelligenceJob(
            bot_name=self.name,
            market=Market.CRYPTO.value,
            source_env="DSH_CRYPTO_INTELLIGENCE_SOURCES",
            watchlist=SIXCELUE_CRYPTO_UNIVERSE,
            default_quantity="0.01",
        )
        self._auditor = StrategyAuditorJob(
            bot_name=self.name,
            market=Market.CRYPTO.value,
            report_kind="intelligence-daily",
        )

    def tick(self, session) -> None:
        session.use("query_positions")
        holdings = self.gateway.get_positions(self.market, account_id=self.account_id)
        self._intelligence.run(session, holdings=holdings)
        super().tick(session)
        self._auditor.run(session)
