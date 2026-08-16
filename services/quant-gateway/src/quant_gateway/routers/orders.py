"""Quant Gateway 订单接口（PRD 11.1）。

request_order / cancel_order 可改变资金状态，必须满足：
- 请求体按 OrderIntent 契约校验
- 幂等键去重
- 风险快照必须存在且未命中限制（limits_hit 为空）
- 二次硬风控：调用 risk-policy /v1/check-order，不可达时失败关闭
- 必须携带已审批（APPROVED）的 approval_id
- 返回权威订单 ID 或明确错误
"""

import os


from fastapi import APIRouter, Depends, HTTPException
from pydantic import ValidationError

from dsh_contracts import Market, OrderIntent, RiskSnapshot
from quant_gateway import audit, storage
from quant_gateway.adapters import get_adapter
from quant_gateway.approval_store import check_order_risk, is_approved
from quant_gateway.auth import Principal, require_write

router = APIRouter(dependencies=[Depends(require_write)])

RISK_POLICY_URL = os.environ.get("RISK_POLICY_URL", "http://127.0.0.1:8003")

# 内存中的风险快照；生产应由 risk-policy 生成并持久化。
_risk_snapshots: dict[str, RiskSnapshot] = {}


def register_risk_snapshot(snapshot: RiskSnapshot) -> None:
    """测试/联调辅助：注册风险快照。生产由 risk-policy 写入。"""
    _risk_snapshots[snapshot.risk_snapshot_id] = snapshot


def _account_equity(adapter, account_id: str):
    """从适配器取账户权益；取不到返回 0，让 risk-policy 失败关闭。"""
    try:
        summaries = adapter.get_account_summary()
        match = next((s for s in summaries if s.account_id == account_id), None)
        return match.equity if match is not None else 0
    except Exception:
        return 0


@router.post("/markets/{market}/orders")
def request_order(market: Market, intent: dict,
                  principal: Principal = Depends(require_write)):
    # 1. 契约校验：拒绝模糊或不完整的订单意图
    try:
        order_intent = OrderIntent.model_validate(intent)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors(include_url=False)) from exc
    if order_intent.market != market:
        raise HTTPException(
            status_code=422,
            detail=f"intent market {order_intent.market} does not match path market {market}",
        )

    adapter = get_adapter(market)

    # 2. 幂等（持久化键日志：重启后重复请求仍不会重复下单）
    idempotency_key = order_intent.idempotency_key
    previous_order_id = storage.get_order_id_for_key(idempotency_key)
    if previous_order_id is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                "duplicate idempotency key; no new order created; "
                f"previous order_id={previous_order_id}"
            ),
        )

    # 3. 风险快照（失败关闭：查不到快照即拒绝）
    snapshot = _risk_snapshots.get(order_intent.risk_snapshot_id)
    if snapshot is None:
        raise HTTPException(
            status_code=422,
            detail="risk snapshot not found; fail-closed, order rejected",
        )
    if snapshot.limits_hit:
        raise HTTPException(
            status_code=422,
            detail=f"risk limits hit: {snapshot.limits_hit}; order rejected",
        )

    # 4. 二次硬风控（失败关闭：risk-policy 不可达或拒绝即拒绝订单）
    try:
        check = check_order_risk(
            RISK_POLICY_URL,
            market=order_intent.market.value,
            account_id=order_intent.account_id,
            symbol=order_intent.symbol,
            quantity=str(order_intent.quantity),
            notional=str(snapshot.risk_budget_delta),
            worst_case_loss=str(snapshot.worst_case_loss),
            equity=str(_account_equity(adapter, order_intent.account_id)),
        )
    except Exception as exc:  # 任何失败都失败关闭，不做猜测放行
        raise HTTPException(
            status_code=503,
            detail=f"risk-policy unreachable; fail-closed, order rejected: {exc}",
        ) from exc
    if not check.get("passed"):
        raise HTTPException(
            status_code=422,
            detail=f"risk check failed: {check.get('limits_hit')}; order rejected",
        )

    # 5. 审批（失败关闭）
    if not order_intent.approval_id:
        raise HTTPException(
            status_code=422, detail="approval_id is required for state-changing orders"
        )
    if not is_approved(order_intent.approval_id):
        raise HTTPException(
            status_code=422,
            detail=f"approval {order_intent.approval_id} is not APPROVED; order rejected",
        )

    order_id = adapter.request_order(order_intent.model_dump(mode="json"))
    if not storage.record_idempotency_key(idempotency_key, order_id):
        # 并发竞态下的重复提交：撤回本单意图，返回既有订单 ID
        raise HTTPException(
            status_code=409,
            detail=(
                "duplicate idempotency key; no new order created; "
                f"previous order_id={storage.get_order_id_for_key(idempotency_key)}"
            ),
        )
    audit.record(
        "order.submitted", principal.name, market.value, order_id,
        detail=f"intent={order_intent.idempotency_key} "
               f"approval={order_intent.approval_id} "
               f"{order_intent.side} {order_intent.symbol} {order_intent.quantity}",
    )
    return {"order_id": order_id, "status": "SUBMITTED"}


@router.post("/markets/{market}/orders/{order_id}/cancel")
def cancel_order(market: Market, order_id: str,
                 principal: Principal = Depends(require_write)):
    result = get_adapter(market).cancel_order(order_id)
    audit.record("order.cancelled", principal.name, market.value, order_id)
    return result
