"""适配器注册表。

失败关闭原则（NFR-002）：未注册适配器的市场一律返回 503，
不返回猜测数据，也不允许资金动作。
"""

from fastapi import HTTPException

from dsh_contracts import Market
from quant_gateway.adapters.base import MarketAdapter

_adapters: dict[Market, MarketAdapter] = {}


def register_adapter(market: Market, adapter: MarketAdapter) -> None:
    _adapters[market] = adapter


def get_adapter(market: Market) -> MarketAdapter:
    adapter = _adapters.get(market)
    if adapter is None:
        raise HTTPException(
            status_code=503,
            detail=f"market {market} has no adapter configured; failing closed",
        )
    return adapter
