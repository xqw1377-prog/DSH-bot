"""市场策略接口与实现。

MarketPolicy 承载市场特有规则：交易时段、最小交易单位、价格限制、
T+1 等。TradeExecutionCore 在固定接入点调用，市场差异不得散落到
执行流程里。
"""

from datetime import UTC, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_DOWN

from dsh_contracts import Market


class MarketPolicy:
    """默认策略：7×24、无整手、无涨跌停（数字资产现货近似）。"""

    # 可注入时钟（测试用）
    clock:staticmethod = staticmethod(lambda: datetime.now(UTC))

    def session_blocked(self) -> str | None:
        """返回 None 表示可交易；否则返回不可交易原因。"""
        return None

    def default_quantity(self, signal: dict) -> str:
        return "0.01"

    def validate_order(self, market: Market, quantity: str, est_price,
                       symbol: str | None = None) -> str | None:
        """下单前校验（数量/价格规则）。返回 None 通过。"""
        return None

    def round_quantity(self, quantity: Decimal) -> Decimal:
        return quantity


CST = timezone(timedelta(hours=8))


class AStockMarketPolicy(MarketPolicy):
    """A 股规则：交易时段与午休、周末休市、100 股整手、±10% 涨跌停、T+1。

    T+1 说明：当日买入的份额当日不可卖。执行层面由量化系统按
    available_quantity 控制（冻结当日买入），本策略在 SELL 时提示
    校验可用数量；Paper 适配器的 available_quantity 即代表可卖余额。
    """

    lot_size = Decimal("100")
    price_limit_pct = Decimal("0.10")
    # Paper 参考昨收价（真实系统应从行情快照取）
    prev_close: dict[str, Decimal] = {
        "600519": Decimal("1680.50"),
        "600519.SH": Decimal("1680.50"),
    }

    def now_cst(self) -> datetime:
        return datetime.now(CST)

    def session_blocked(self, now: datetime | None = None) -> str | None:
        now = now or self.now_cst()
        if now.weekday() >= 5:
            return f"周末休市（{now:%Y-%m-%d}）"
        t = now.time()
        morning = time(9, 30) <= t <= time(11, 30)
        afternoon = time(13, 0) <= t <= time(15, 0)
        if morning or afternoon:
            return None
        if time(11, 30) < t < time(13, 0):
            return "午间休市（11:30-13:00）"
        return f"集合竞价/闭市时段（{t:%H:%M}）"

    def default_quantity(self, signal: dict) -> str:
        return "100"

    def round_quantity(self, quantity: Decimal) -> Decimal:
        return (quantity / self.lot_size).to_integral_value(
            rounding=ROUND_DOWN) * self.lot_size

    def validate_order(self, market: Market, quantity: str,
                       est_price, symbol: str | None = None) -> str | None:
        qty = Decimal(str(quantity))
        if qty == 0:
            return "数量为零"
        if qty % self.lot_size != 0:
            return f"A 股必须整手（{int(self.lot_size)} 股）"
        if est_price is None:
            return None  # 无参考价时由 venue 决定（失败关闭在网关侧）
        price = Decimal(str(est_price))
        ref = self.prev_close.get(symbol or "")
        if ref is None:
            return None
        up, down = ref * (1 + self.price_limit_pct), ref * (1 - self.price_limit_pct)
        if price > up:
            return f"超过涨停价 {up}（现价 {price}）"
        if price < down:
            return f"低于跌停价 {down}（现价 {price}）"
        return None

    def can_sell_today(self, bought_today: bool) -> bool:
        """T+1：当日买入不可当日卖出。"""
        return not bought_today
