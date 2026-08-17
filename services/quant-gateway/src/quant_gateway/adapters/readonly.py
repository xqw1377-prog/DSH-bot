"""只读适配器：包装真实/Paper 适配器，禁止任何资金动作。

Shadow 是 Bot 运行模式，不是第三种交易所。本包装用于
「真实行情/账户只读」接入，写入一律失败关闭。
"""

from __future__ import annotations

from fastapi import HTTPException

from quant_gateway.adapters.base import MarketAdapter


class ReadOnlyAdapter(MarketAdapter):
    def __init__(self, inner: MarketAdapter) -> None:
        self._inner = inner

    def get_health(self):
        return self._inner.get_health()

    def get_positions(self, account_id: str | None = None):
        return self._inner.get_positions(account_id)

    def get_account_summary(self):
        return self._inner.get_account_summary()

    def get_signals(self):
        return self._inner.get_signals()

    def preview_order(self, intent):
        return self._inner.preview_order(intent)

    def request_order(self, intent) -> str:
        raise HTTPException(
            status_code=403,
            detail="read-only adapter refuses request_order; use Shadow bot mode",
        )

    def get_order_status(self, order_id: str) -> dict:
        return self._inner.get_order_status(order_id)

    def cancel_order(self, order_id: str) -> dict:
        raise HTTPException(status_code=403, detail="read-only adapter refuses cancel")

    def pause_strategy(self, strategy_id: str) -> None:
        raise HTTPException(status_code=403, detail="read-only adapter refuses pause")

    def resume_strategy(self, strategy_id: str) -> None:
        raise HTTPException(status_code=403, detail="read-only adapter refuses resume")

    def emergency_stop(self, account_id: str | None = None) -> None:
        raise HTTPException(
            status_code=403, detail="read-only adapter refuses emergency_stop"
        )

    def find_order_by_idempotency_key(self, key: str) -> dict | None:
        finder = getattr(self._inner, "find_order_by_idempotency_key", None)
        if finder is None:
            return None
        return finder(key)
