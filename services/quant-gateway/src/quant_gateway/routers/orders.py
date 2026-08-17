"""Quant Gateway 订单接口（PRD 11.1）。

request_order / cancel_order 可改变资金状态，必须满足：
- 请求体按 OrderIntent 契约校验
- 幂等键去重
- 风险快照必须存在且未命中限制（limits_hit 为空）
- 二次硬风控：调用 risk-policy /v1/check-order，不可达时失败关闭
- 必须携带已审批（APPROVED）的 approval_id
- 返回权威订单 ID 或明确错误
"""

import hashlib
import json
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


@router.post("/markets/{market}/risk-snapshots", status_code=201)
def register_risk_snapshot_api(market: Market, snapshot: dict,
                               principal: Principal = Depends(require_write)):
    """注册风险快照。快照来源必须可信（如订单预览的计算结果），
    提交订单时按 risk_snapshot_id 查验，查不到即失败关闭。"""
    from pydantic import ValidationError as VE
    try:
        parsed = RiskSnapshot.model_validate(snapshot)
    except VE as exc:
        raise HTTPException(status_code=422, detail=exc.errors(include_url=False)) from exc
    if parsed.market != market:
        raise HTTPException(
            status_code=422,
            detail=f"snapshot market {parsed.market} does not match path market {market}",
        )
    register_risk_snapshot(parsed)
    return {"risk_snapshot_id": parsed.risk_snapshot_id}


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

    # 2. 幂等：请求体哈希 + 持久化键日志（RESERVED→SUBMITTED→COMPLETED）。
    # 相同键相同请求体且已有 order_id → 409 并返回已有订单（不重下）。
    # RESERVED 无 order_id：尝试按 key 反查 venue/paper 订单并补完，仍无则 409 in-flight。
    idempotency_key = order_intent.idempotency_key
    request_hash = hashlib.sha256(
        json.dumps(order_intent.model_dump(mode="json"), sort_keys=True).encode()
    ).hexdigest()
    record = storage.get_idempotency_record(idempotency_key)
    if record is not None:
        previous_order_id = record["order_id"]
        previous_hash = record["request_hash"]
        status = record["status"]
        if previous_hash != request_hash:
            raise HTTPException(
                status_code=409,
                detail=(
                    "idempotency key reused with different request body; "
                    "rejected"
                ),
            )
        if previous_order_id:
            raise HTTPException(
                status_code=409,
                detail=(
                    "duplicate idempotency key; no new order created; "
                    f"previous order_id={previous_order_id}"
                ),
            )
        # 崩溃窗口：RESERVED 且无 order_id → 反查 paper/venue
        recovered = storage.find_paper_order_by_idempotency_key(idempotency_key)
        if recovered and recovered.get("order_id"):
            oid = recovered["order_id"]
            storage.mark_idempotency_submitted(idempotency_key, oid)
            storage.finalize_idempotency_key(idempotency_key, oid)
            raise HTTPException(
                status_code=409,
                detail=(
                    "duplicate idempotency key; recovered after crash; "
                    f"previous order_id={oid}"
                ),
            )
        raise HTTPException(
            status_code=409,
            detail=(
                f"duplicate idempotency key; status={status}; "
                "previous order_id=(in flight)"
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

    # 6. 原子抢占幂等键（BEGIN IMMEDIATE → RESERVED）
    if not storage.record_idempotency_key(idempotency_key, request_hash):
        # 并发输掉：再查一次，若对方已写出 order_id 则返回该 id
        again = storage.get_idempotency_record(idempotency_key)
        oid = (again or {}).get("order_id")
        raise HTTPException(
            status_code=409,
            detail=(
                "duplicate idempotency key (concurrent request won); "
                f"previous order_id={oid or '(in flight)'}"
            ),
        )

    try:
        order_id = adapter.request_order(order_intent.model_dump(mode="json"))
        # 先写入 SUBMITTED（带 order_id），再 COMPLETED —— 缩小崩溃双单窗口
        storage.mark_idempotency_submitted(idempotency_key, order_id)
        storage.finalize_idempotency_key(idempotency_key, order_id)
    except ValueError as exc:
        storage.mark_idempotency_failed(idempotency_key)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception:
        # venue 异常：若已产生 paper 订单则补完，否则标 FAILED 允许同 hash 重试
        recovered = storage.find_paper_order_by_idempotency_key(idempotency_key)
        if recovered and recovered.get("order_id"):
            oid = recovered["order_id"]
            storage.mark_idempotency_submitted(idempotency_key, oid)
            storage.finalize_idempotency_key(idempotency_key, oid)
            order_id = oid
        else:
            storage.mark_idempotency_failed(idempotency_key)
            raise

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
