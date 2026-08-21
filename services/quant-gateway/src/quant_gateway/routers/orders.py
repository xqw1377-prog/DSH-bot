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

from dsh_contracts import ApprovalStatus, Market, OrderIntent, RiskSnapshot
from quant_gateway import audit, storage
from quant_gateway.adapters import get_adapter
from quant_gateway.approval_store import (
    ClaimStatus,
    check_order_risk,
    claim_order_reservation,
    compute_intent_digest,
    get_approval,
)
from quant_gateway.auth import Principal, require_write
from quant_gateway.errors import structured_error

router = APIRouter(dependencies=[Depends(require_write)])

# RESERVED 无 order_id 的在途宽限：窗口内视为并发在途，不做恢复判定
SUBMISSION_UNKNOWN_GRACE_SECONDS = 30.0


def _idempotency_age_seconds(updated_at: str | None) -> float | None:
    from datetime import UTC, datetime
    if not updated_at:
        return None
    try:
        return (datetime.now(UTC) - datetime.fromisoformat(updated_at)).total_seconds()
    except ValueError:
        return None

RISK_POLICY_URL = os.environ.get("RISK_POLICY_URL", "http://127.0.0.1:8003")


def digest_from_intent(intent: OrderIntent) -> str:
    """从订单意图计算绑定摘要，与审批创建时的 binding 摘要比对。"""
    return compute_intent_digest({
        "market": intent.market.value,
        "account_id": intent.account_id,
        "symbol": intent.symbol,
        "side": intent.side.value,
        "order_type": intent.order_type,
        "quantity": str(intent.quantity),
        "limit_price": (
            str(intent.limit_price) if intent.limit_price is not None else None
        ),
        "strategy_version": intent.strategy_version,
        "signal_snapshot_id": intent.signal_snapshot_id,
        "risk_snapshot_id": intent.risk_snapshot_id,
        "valid_until": intent.valid_until.isoformat(),
    })


def _precheck_approval(intent: OrderIntent, intent_digest: str) -> None:
    """快速失败预检（只读）。权威门禁在 claim_order_reservation。"""
    if not intent.approval_id:
        raise structured_error(
            422,
            error_code="APPROVAL_REQUIRED",
            phase="PRE_SUBMIT",
            retryable=False,
            submission_unknown=False,
            message="approval_id is required for state-changing orders",
        )
    approval = get_approval(intent.approval_id)
    if approval is None:
        raise structured_error(
            422,
            error_code="APPROVAL_NOT_APPROVED",
            phase="PRE_SUBMIT",
            retryable=False,
            submission_unknown=False,
            message=(
                f"approval {intent.approval_id} not found or expired; order rejected"
            ),
        )
    if approval.status != ApprovalStatus.APPROVED:
        if approval.status in (
            ApprovalStatus.CONSUMING,
            ApprovalStatus.CONSUMED,
        ):
            raise structured_error(
                409,
                error_code=(
                    "APPROVAL_IN_FLIGHT"
                    if approval.status == ApprovalStatus.CONSUMING
                    else "APPROVAL_ALREADY_CONSUMED"
                ),
                phase="SUBMITTING",
                retryable=False,
                submission_unknown=False,
                message=(
                    f"approval {intent.approval_id} is "
                    f"{approval.status.value}; already consumed for a previous order"
                ),
            )
        raise structured_error(
            422,
            error_code="APPROVAL_NOT_APPROVED",
            phase="PRE_SUBMIT",
            retryable=False,
            submission_unknown=False,
            message=(
                f"approval {intent.approval_id} status is {approval.status.value}; "
                "not APPROVED; order rejected"
            ),
        )
    if approval.intent_digest is None:
        raise structured_error(
            409,
            error_code="APPROVAL_UNBOUNDED",
            phase="PRE_SUBMIT",
            retryable=False,
            submission_unknown=False,
            message=(
                f"approval {intent.approval_id} carries no order intent binding; "
                "cannot authorize an order"
            ),
        )
    if approval.intent_digest != intent_digest:
        raise structured_error(
            409,
            error_code="APPROVAL_INTENT_MISMATCH",
            phase="PRE_SUBMIT",
            retryable=False,
            submission_unknown=False,
            message=(
                f"order intent does not match approved binding for "
                f"{intent.approval_id}; order rejected"
            ),
        )


_CLAIM_ERRORS = {
    ClaimStatus.APPROVAL_NOT_FOUND: "APPROVAL_NOT_APPROVED",
    ClaimStatus.APPROVAL_NOT_APPROVED: "APPROVAL_ALREADY_CONSUMED",
    ClaimStatus.APPROVAL_EXPIRED: "APPROVAL_EXPIRED",
    ClaimStatus.APPROVAL_INTENT_MISMATCH: "APPROVAL_INTENT_MISMATCH",
    ClaimStatus.APPROVAL_UNBOUNDED: "APPROVAL_UNBOUNDED",
    ClaimStatus.IDEMPOTENCY_IN_FLIGHT: "IDEMPOTENCY_IN_FLIGHT",
}


def _raise_claim_failure(status: ClaimStatus, message: str) -> None:
    retryable = status == ClaimStatus.IDEMPOTENCY_IN_FLIGHT
    raise structured_error(
        409,
        error_code=_CLAIM_ERRORS[status],
        phase="SUBMITTING",
        retryable=retryable,
        submission_unknown=retryable,
        message=message,
    )


def register_risk_snapshot(snapshot: RiskSnapshot) -> None:
    """注册风险快照（持久化，多 worker 可见）。生产由 risk-policy 写入。"""
    storage.save_risk_snapshot(
        snapshot.risk_snapshot_id, snapshot.market.value,
        snapshot.model_dump(mode="json"),
    )


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
        raise structured_error(
            422,
            error_code="INTENT_INVALID",
            phase="PRE_SUBMIT",
            retryable=False,
            submission_unknown=False,
            message=str(exc.errors(include_url=False)),
        ) from exc
    if order_intent.market != market:
        raise structured_error(
            422,
            error_code="MARKET_MISMATCH",
            phase="PRE_SUBMIT",
            retryable=False,
            submission_unknown=False,
            message=(
                f"intent market {order_intent.market} does not match path market {market}"
            ),
        )

    adapter = get_adapter(market)

    # 2. 幂等：请求体哈希 + 持久化键日志。
    # 相同键相同请求体 → 409 并返回已有订单；相同键不同请求体 → 409 冲突。
    idempotency_key = order_intent.idempotency_key
    request_hash = hashlib.sha256(
        json.dumps(order_intent.model_dump(mode="json"), sort_keys=True).encode()
    ).hexdigest()
    entry = storage.get_idempotency_entry(idempotency_key)
    if entry is not None:
        previous_order_id, previous_hash = entry
        if previous_hash != request_hash:
            raise structured_error(
                409,
                error_code="IDEMPOTENCY_CONFLICT",
                phase="PRE_SUBMIT",
                retryable=False,
                submission_unknown=False,
                message=(
                    "idempotency key reused with different request body; rejected"
                ),
            )
        if previous_order_id:
            raise structured_error(
                409,
                error_code="DUPLICATE_ORDER",
                phase="PRE_SUBMIT",
                retryable=False,
                submission_unknown=False,
                message=(
                    "duplicate idempotency key; no new order created; "
                    f"previous order_id={previous_order_id}"
                ),
            )
        # SUBMISSION_UNKNOWN：venue 可能已接单但网关在写库前崩溃。
        # 仅当键已老化（超过在途窗口）才做恢复：新鲜的 RESERVED 属于并发在途，
        # venue 查不到不代表未接单。恢复顺序：找到即认领；明确不存在才释放重试。
        record = storage.get_idempotency_record(idempotency_key) or {}
        age_seconds = _idempotency_age_seconds(record.get("updated_at"))
        if age_seconds is None or age_seconds < SUBMISSION_UNKNOWN_GRACE_SECONDS:
            raise structured_error(
                409,
                error_code="IDEMPOTENCY_IN_FLIGHT",
                phase="SUBMITTING",
                retryable=True,
                submission_unknown=True,
                message=(
                    "idempotency key in flight (RESERVED without order_id); "
                    "retry after grace period"
                ),
            )
        recovered = adapter.find_order_by_idempotency_key(idempotency_key)
        if recovered is not None and recovered.get("order_id"):
            recovered_id = recovered["order_id"]
            storage.finalize_idempotency_key(idempotency_key, recovered_id)
            audit.record(
                "order.submission_recovered", principal.name, market.value,
                recovered_id,
                detail=f"intent={idempotency_key} recovered from venue",
            )
            return {"order_id": recovered_id, "status": "SUBMITTED",
                    "recovered": True}
        if recovered is None:
            consistency = getattr(adapter, "order_lookup_consistency", "UNSUPPORTED")
            if consistency != "STRONG":
                raise structured_error(
                    409,
                    error_code="SUBMISSION_UNKNOWN",
                    phase="SUBMITTING",
                    retryable=False,
                    submission_unknown=True,
                    message=(
                        "submission unknown: venue lookup is not strongly consistent; "
                        "resubmission blocked"
                    ),
                )
            # STRONG：venue 确认从未接受，释放幂等键后按新单继续走完整门禁
            storage.mark_idempotency_failed(idempotency_key)
        else:
            # 查询结果异常（无 order_id 的记录）：保持占用，禁止重提
            raise structured_error(
                409,
                error_code="SUBMISSION_UNKNOWN",
                phase="SUBMITTING",
                retryable=False,
                submission_unknown=True,
                message=(
                    "submission unknown: idempotency key occupied without "
                    "order_id; venue lookup inconclusive; resubmission blocked"
                ),
            )

    # 3. 风险快照（失败关闭：查不到快照即拒绝）
    raw = storage.get_risk_snapshot(order_intent.risk_snapshot_id)
    snapshot = RiskSnapshot.model_validate(raw) if raw is not None else None
    if snapshot is None:
        raise structured_error(
            422,
            error_code="RISK_SNAPSHOT_MISSING",
            phase="PRE_SUBMIT",
            retryable=False,
            submission_unknown=False,
            message="risk snapshot not found; fail-closed, order rejected",
        )
    if snapshot.limits_hit:
        raise structured_error(
            422,
            error_code="RISK_LIMITS_HIT",
            phase="PRE_SUBMIT",
            retryable=False,
            submission_unknown=False,
            message=f"risk limits hit: {snapshot.limits_hit}; order rejected",
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
    except Exception as exc:  # venue 尚未调用
        raise structured_error(
            503,
            error_code="RISK_POLICY_UNAVAILABLE",
            phase="PRE_SUBMIT",
            retryable=True,
            submission_unknown=False,
            message=f"risk-policy unreachable; fail-closed, order rejected: {exc}",
        ) from exc
    if check.get("kill_switch") or check.get("severity") == "CRITICAL":
        try:
            audit.record(
                "kill_switch.requested", principal.name, market.value,
                order_intent.account_id,
                detail=f"risk-policy CRITICAL {check.get('limits_hit')}",
            )
            adapter.emergency_stop(account_id=order_intent.account_id)
            audit.record(
                "kill_switch.succeeded", principal.name, market.value,
                order_intent.account_id,
                detail="emergency_stop engaged after CRITICAL risk check",
            )
        except Exception as exc:
            audit.record(
                "kill_switch.failed", principal.name, market.value,
                order_intent.account_id,
                detail=str(exc),
            )
    if not check.get("passed"):
        raise structured_error(
            422,
            error_code="RISK_CHECK_REJECTED",
            phase="PRE_SUBMIT",
            retryable=False,
            submission_unknown=False,
            message=f"risk check failed: {check.get('limits_hit')}; order rejected",
        )

    # 5. 审批（失败关闭 + 意图绑定）：快速失败预检；权威门禁见第 7 步原子消费
    intent_digest = digest_from_intent(order_intent)
    _precheck_approval(order_intent, intent_digest)

    # 6. Kill Switch / 通道降级：禁止新提交，但不阻断已提交订单的幂等认领
    health = adapter.get_health()
    if not getattr(health, "system_ok", True) or not getattr(
        health, "trading_channel_ok", True
    ):
        raise structured_error(
            409,
            error_code="TRADING_HALTED",
            phase="PRE_SUBMIT",
            retryable=True,
            submission_unknown=False,
            message=(
                getattr(health, "detail", None)
                or "trading channel not ok; order rejected"
            ),
        )

    # 7. 原子抢占：审批消费 + 幂等键声明在同一事务（并发唯一胜者）
    claim_status, claim_message = claim_order_reservation(
        order_intent.approval_id,
        intent_digest,
        idempotency_key,
        request_hash,
    )
    if claim_status != ClaimStatus.OK:
        _raise_claim_failure(claim_status, claim_message)

    try:
        order_id = adapter.request_order(order_intent.model_dump(mode="json"))
    except HTTPException:
        raise
    except ValueError as exc:
        raise structured_error(
            422,
            error_code="VENUE_REJECTED",
            phase="VENUE",
            retryable=False,
            submission_unknown=False,
            message=str(exc),
        ) from exc
    except Exception as exc:
        raise structured_error(
            503,
            error_code="VENUE_SUBMIT_UNKNOWN",
            phase="SUBMITTING",
            retryable=False,
            submission_unknown=True,
            message=str(exc),
        ) from exc
    try:
        storage.finalize_idempotency_key(idempotency_key, order_id)
    except Exception as exc:
        raise structured_error(
            503,
            error_code="LOCAL_PERSIST_FAILED",
            phase="POST_SUBMIT",
            retryable=True,
            submission_unknown=True,
            message=str(exc),
        ) from exc
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
