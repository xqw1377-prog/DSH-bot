"""A 股市场策略测试：交易时段/午休/周末/整手/涨跌停/T+1。"""

from datetime import datetime
from decimal import Decimal

import pytest
from dsh_contracts import Market
from dsh_trade_core import AStockMarketPolicy

P = AStockMarketPolicy()


# ---- 交易时段 ----

@pytest.mark.parametrize("when,ok", [
    (datetime(2026, 8, 17, 9, 30), None),    # 周一开盘
    (datetime(2026, 8, 17, 11, 30), None),   # 早盘收尾
    (datetime(2026, 8, 17, 13, 0), None),    # 午后开盘
    (datetime(2026, 8, 17, 15, 0), None),    # 收盘
])
def test_trading_window_open(when, ok):
    assert P.session_blocked(when) is None


@pytest.mark.parametrize("when,fragment", [
    (datetime(2026, 8, 17, 11, 31), "午间休市"),
    (datetime(2026, 8, 17, 12, 30), "午间休市"),
    (datetime(2026, 8, 17, 9, 0), "闭市"),
    (datetime(2026, 8, 17, 15, 1), "闭市"),
    (datetime(2026, 8, 15, 10, 0), "周末休市"),  # 周六
    (datetime(2026, 8, 16, 10, 0), "周末休市"),  # 周日
])
def test_trading_window_blocked(when, fragment):
    blocked = P.session_blocked(when)
    assert blocked is not None and fragment in blocked


# ---- 整手 ----

def test_lot_rounding():
    assert P.round_quantity(Decimal("150")) == Decimal("100")
    assert P.round_quantity(Decimal("999")) == Decimal("900")
    assert P.round_quantity(Decimal("200")) == Decimal("200")


def test_odd_lot_rejected():
    problem = P.validate_order(Market.A_SHARE, "150", None, "600519")
    assert problem and "整手" in problem
    assert P.validate_order(Market.A_SHARE, "200", None, "600519") is None


# ---- 涨跌停（昨收 1680.50，±10%）----

def test_price_limit():
    up = Decimal("1848.55")
    down = Decimal("1512.45")
    assert P.validate_order(Market.A_SHARE, "100", up, "600519") is None
    blocked_up = P.validate_order(Market.A_SHARE, "100", up + 1, "600519")
    assert blocked_up and "涨停" in blocked_up
    blocked_down = P.validate_order(Market.A_SHARE, "100", down - 1, "600519")
    assert blocked_down and "跌停" in blocked_down


# ---- T+1 ----

def test_t_plus_one():
    assert P.can_sell_today(bought_today=False) is True
    assert P.can_sell_today(bought_today=True) is False


# ---- 默认数量 ----

def test_default_quantity_is_one_lot():
    assert P.default_quantity({}) == "100"
