"""Quant Gateway 只读接口（PRD 11.1）。

这些接口不改变资金状态，但必须通过适配器请求现有量化系统，
不能读取生产数据库或券商/交易所密钥。
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import ValidationError

from dsh_contracts import Market, OrderIntent
from quant_gateway import storage
from quant_gateway.adapters import get_adapter
from quant_gateway.auth import require_read

router = APIRouter(dependencies=[Depends(require_read)])


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


@router.get("/markets/{market}/watch")
def get_watch(market: Market):
    """筛选结果与被源系统挡掉的候选。不是正式信号，不能据此下单。"""
    adapter = get_adapter(market)
    getter = getattr(adapter, "get_watch", None)
    if getter is None:
        return {"screen_results": [], "rejected_candidates": []}
    return getter()


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
    preview = get_adapter(market).preview_order(order_intent.model_dump(mode="json"))
    # 风险快照的唯一签发点：值由适配器按权威持仓/价格计算，网关持久化
    # 并绑定该订单意图。Bot 不能自报风控事实（无注册端点）。
    risk = preview.get("risk") if isinstance(preview, dict) else None
    if isinstance(risk, dict) and risk.get("risk_snapshot_id"):
        from dsh_contracts import RiskSnapshot
        from quant_gateway.routers.orders import (
            register_risk_snapshot,
            snapshot_binding_digest,
        )
        snapshot = RiskSnapshot.model_validate(risk)
        register_risk_snapshot(snapshot, snapshot_binding_digest(order_intent))
    return preview


@router.get("/markets/{market}/orders/{order_id}")
def get_order_status(market: Market, order_id: str):
    return get_adapter(market).get_order_status(order_id)


@router.get("/idempotency-keys/{key}")
def get_idempotency_key(key: str):
    """按幂等键查询已产生的订单。崩溃恢复 / 409 冲突时用于认领既有订单，
    绝不用于重新下单。"""
    entry = storage.get_idempotency_entry(key)
    if entry is None:
        raise HTTPException(status_code=404, detail="idempotency key not found")
    order_id, request_hash = entry
    return {"key": key, "order_id": order_id, "request_hash": request_hash}
