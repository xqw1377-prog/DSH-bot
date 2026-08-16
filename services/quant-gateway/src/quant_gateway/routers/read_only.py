"""Quant Gateway 只读接口（PRD 11.1）。

这些接口不改变资金状态，但必须通过适配器请求现有量化系统，
不能读取生产数据库或券商/交易所密钥。
"""

from fastapi import APIRouter, HTTPException
from pydantic import ValidationError

from dsh_contracts import Market, OrderIntent
from quant_gateway.adapters import get_adapter

router = APIRouter()


@router.get("/markets/{market}/health")
def get_health(market: Market):
    return get_adapter(market).get_health()


@router.get("/markets/{market}/positions")
def get_positions(market: Market, account_id: str | None = None):
    return get_adapter(market).get_positions(account_id=account_id)


@router.get("/markets/{market}/accounts")
def get_account_summary(market: Market):
    return get_adapter(market).get_account_summary()


@router.get("/markets/{market}/signals")
def get_signals(market: Market):
    return get_adapter(market).get_signals()


@router.post("/markets/{market}/orders/preview")
def preview_order(market: Market, intent: dict):
    try:
        order_intent = OrderIntent.model_validate(intent)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors(include_url=False)) from exc
    if order_intent.market != market:
        raise HTTPException(
            status_code=422,
            detail=f"intent market {order_intent.market} does not match path market {market}",
        )
    return get_adapter(market).preview_order(order_intent.model_dump(mode="json"))


@router.get("/markets/{market}/orders/{order_id}")
def get_order_status(market: Market, order_id: str):
    return get_adapter(market).get_order_status(order_id)
