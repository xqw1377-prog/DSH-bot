"""A 股 Bot：与 Crypto 共用 TradeExecutionCore，Paper 先、LLM 不进。"""

from collections.abc import Callable
from datetime import datetime
from decimal import Decimal

from dsh_contracts import Market
from dsh_runtime import BotIntelligenceJob, StrategyAuditorJob
from dsh_runtime.execution import TradeExecutionCore


class _ASharePolicy:
    """整手 / 涨跌停由执行核调用；交易时段以 health.market_session 为准。"""

    price_limit_pct = Decimal("0.10")
    # Paper 参考昨收价（真实系统应从行情快照取）
    prev_close = {
        "600519": Decimal("1680.50"),
        "600519.SH": Decimal("1680.50"),
    }

    def session_blocked(self, now=None) -> str | None:
        return None

    def default_quantity(self, signal: dict) -> str:
        return str(signal.get("quantity") or "100")

    def validate_order(self, market, quantity: str, est_price, symbol=None) -> str | None:
        qty = Decimal(str(quantity or "0"))
        if qty == 0:
            return "数量为零"
        if qty % Decimal("100") != 0:
            return "A 股必须整手（100 股）"
        if est_price is not None:
            price = Decimal(str(est_price))
            ref = self.prev_close.get(symbol or "")
            if ref is not None:
                up = ref * (1 + self.price_limit_pct)
                down = ref * (1 - self.price_limit_pct)
                if price > up:
                    return f"超过涨停价 {up}（现价 {price}）"
                if price < down:
                    return f"低于跌停价 {down}（现价 {price}）"
        return None


class AShareAgent(TradeExecutionCore):
    name = "a-stock-bot"

    def __init__(
        self,
        gateway,
        approvals,
        account_id: str,
        min_strength: float = 0.6,
        mode: str = "paper",
        now_fn: Callable[[], datetime] | None = None,
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
            now_fn=now_fn,
            policy=_ASharePolicy(),
        )
        self._intelligence = BotIntelligenceJob(
            bot_name=self.name,
            market=Market.A_SHARE.value,
            source_env="DSH_A_SHARE_INTELLIGENCE_SOURCES",
            watchlist=(),
            default_quantity="100",
        )
        self._auditor = StrategyAuditorJob(
            bot_name=self.name,
            market=Market.A_SHARE.value,
            report_kind="intelligence-daily",
        )

    def tick(self, session) -> None:
        session.use("query_positions")
        holdings = self.gateway.get_positions(self.market, account_id=self.account_id)
        self._intelligence.run(session, holdings=holdings)
        super().tick(session)
        self._auditor.run(session)
