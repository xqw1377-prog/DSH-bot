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
from datetime import UTC, datetime

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
from quant_gateway.auth import (
    Principal,
    require_cancel_service,
    require_market_runtime_service,
    require_write,
)
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

# 风险快照有效期：预览时签发，超龄快照不得用于下单
RISK_SNAPSHOT_TTL_SECONDS = 600.0


def snapshot_now() -> datetime:
    """新鲜度校验的基准时钟；测试可注入确定性时钟。"""
    return datetime.now(UTC)


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


def snapshot_binding_digest(intent: OrderIntent) -> str:
    """快照绑定摘要：不含 valid_until（重预览会刷新时效，但绑定不变）。"""
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

def register_risk_snapshot(snapshot: RiskSnapshot,
                           binding_digest: str | None = None) -> bool:
    """注册网关签发的风险快照（持久化、不可覆盖）。

    仅两条合法来源：
    - 订单预览时由适配器按权威持仓/价格计算（见 read_only.preview_order）
    - 测试直接注入
    Bot 不得自报风控事实；binding_digest 把快照钉在单一订单意图上。
    """
    payload = snapshot.model_dump(mode="json")
    if binding_digest is not None:
        payload["binding_digest"] = binding_digest
    return storage.save_risk_snapshot(
        snapshot.risk_snapshot_id, snapshot.market.value, payload
    )


def _account_equity(adapter, account_id: str):
    """从适配器取账户权益；取不到返回 None（数据不可用，非 0）。"""
    try:
        summaries = adapter.get_account_summary()
        match = next((s for s in summaries if s.account_id == account_id), None)
        return match.equity if match is not None else None
    except Exception:
        return None


@router.post("/markets/{market}/orders")
def request_order(market: Market, intent: dict,
                  principal: Principal = Depends(require_write)):
    """下单入口：所有拒绝路径统一落审计（网关是审计权威）。

    之前只记成功不记拒绝——风控拦截、审批拒绝、幂等冲突全部不可追溯。
    """
    try:
        return _request_order_impl(market, intent, principal)
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else None
        if detail and detail.get("error_code"):
            audit.record(
                "order.rejected",
                service_principal=principal.name,
                market=market.value,
                subject_id=(
                    str(intent.get("idempotency_key") or "")
                    if isinstance(intent, dict) else ""
                ),
                detail=(
                    f"error_code={detail.get('error_code')} "
                    f"phase={detail.get('phase')} "
                    f"submission_unknown={detail.get('submission_unknown')}: "
                    f"{str(detail.get('message'))[:300]}"
                ),
            )
        raise


def _request_order_impl(market: Market, intent: dict,
                        principal: Principal):
    require_market_runtime_service(principal, market)
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
    # 意图时效：过期意图直接拒绝，不进入任何后续门禁
    try:
        expired = snapshot_now() > order_intent.valid_until
    except TypeError:
        expired = True
    if expired:
        raise structured_error(
            422,
            error_code="INTENT_EXPIRED",
            phase="PRE_SUBMIT",
            retryable=False,
            submission_unknown=False,
            message=(
                f"order intent expired at {order_intent.valid_until.isoformat()}"
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
        if record.get("status") != "FAILED":
            # SUBMISSION_UNKNOWN：venue 可能已接单但网关在写库前崩溃。
            # 仅当键已老化（超过在途窗口）才做恢复：新鲜的 RESERVED 属于并发在途，
            # venue 查不到不代表未接单。恢复顺序：找到即认领；明确不存在才释放重试。
            # （FAILED = 确定性失败如 venue 拒单后释放，不在途：直接重走完整门禁）
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
                    "order.submission_recovered",
                    service_principal=principal.name,
                    market=market.value,
                    subject_id=recovered_id,
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

    # 3. 风险快照（失败关闭）：必须是网关在预览时签发的快照，
    #    且归属（市场/账户）、绑定摘要、新鲜度全部匹配本订单意图。
    raw = storage.get_risk_snapshot(order_intent.risk_snapshot_id)
    stored_binding = (raw or {}).pop("binding_digest", None) if raw else None
    snapshot = RiskSnapshot.model_validate(raw) if raw is not None else None
    if snapshot is None:
        raise structured_error(
            422,
            error_code="RISK_SNAPSHOT_MISSING",
            phase="PRE_SUBMIT",
            retryable=False,
            submission_unknown=False,
            message=(
                "risk snapshot not found; snapshots are issued by the gateway "
                "at preview time; fail-closed, order rejected"
            ),
        )
    if snapshot.market != order_intent.market or (
        snapshot.account_id != order_intent.account_id
    ):
        raise structured_error(
            422,
            error_code="RISK_SNAPSHOT_MISMATCH",
            phase="PRE_SUBMIT",
            retryable=False,
            submission_unknown=False,
            message=(
                f"risk snapshot belongs to {snapshot.market.value}/"
                f"{snapshot.account_id}, not this order's "
                f"{order_intent.market.value}/{order_intent.account_id}"
            ),
        )
    binding_digest = snapshot_binding_digest(order_intent)
    if stored_binding is not None and stored_binding != binding_digest:
        raise structured_error(
            422,
            error_code="RISK_SNAPSHOT_MISMATCH",
            phase="PRE_SUBMIT",
            retryable=False,
            submission_unknown=False,
            message=(
                "risk snapshot was issued for a different order intent; "
                "snapshots cannot be reused across orders"
            ),
        )
    try:
        snapshot_age = (
            snapshot_now() - snapshot.as_of
        ).total_seconds()
    except (TypeError, ValueError):
        snapshot_age = None
    if snapshot_age is None or snapshot_age > RISK_SNAPSHOT_TTL_SECONDS:
        raise structured_error(
            422,
            error_code="RISK_SNAPSHOT_STALE",
            phase="PRE_SUBMIT",
            retryable=False,
            submission_unknown=False,
            message=(
                f"risk snapshot is stale (age={snapshot_age}s, "
                f"ttl={RISK_SNAPSHOT_TTL_SECONDS}s); re-preview required"
            ),
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
    # 权益读不到 = 数据不可用：只拒绝当前订单，绝不当作 CRITICAL 触发 Kill Switch
    equity = _account_equity(adapter, order_intent.account_id)
    if equity is None:
        audit.record(
            "order.rejected",
            service_principal=principal.name,
            market=market.value,
            subject_id=order_intent.account_id,
            detail="equity unavailable: DATA_UNAVAILABLE, order rejected only",
        )
        raise structured_error(
            422,
            error_code="DATA_UNAVAILABLE",
            phase="PRE_SUBMIT",
            retryable=True,
            submission_unknown=False,
            message=(
                "account equity unavailable; cannot verify risk for this order; "
                "order rejected (kill switch not engaged)"
            ),
        )
    try:
        check = check_order_risk(
            RISK_POLICY_URL,
            market=order_intent.market.value,
            account_id=order_intent.account_id,
            symbol=order_intent.symbol,
            quantity=str(order_intent.quantity),
            notional=str(snapshot.risk_budget_delta),
            worst_case_loss=str(snapshot.worst_case_loss),
            equity=str(equity),
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
                "kill_switch.requested",
                service_principal=principal.name,
                market=market.value,
                subject_id=order_intent.account_id,
                detail=f"risk-policy CRITICAL {check.get('limits_hit')}",
            )
            adapter.emergency_stop(account_id=order_intent.account_id)
            audit.record(
                "kill_switch.succeeded",
                service_principal=principal.name,
                market=market.value,
                subject_id=order_intent.account_id,
                detail="emergency_stop engaged after CRITICAL risk check",
            )
        except Exception as exc:
            audit.record(
                "kill_switch.failed",
                service_principal=principal.name,
                market=market.value,
                subject_id=order_intent.account_id,
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
        # venue 明确拒单 = 确定从未接单：释放幂等键与已消费审批，
        # 允许同一意图(同摘要)重试；换参数重试会被审批意图摘要拦截。
        try:
            storage.mark_idempotency_failed(idempotency_key)
        except Exception as release_exc:  # 释放失败不能吞掉拒单事实
            audit.record(
                "order.release_failed",
                service_principal=principal.name,
                market=market.value,
                subject_id=idempotency_key,
                detail=f"venue rejected but release failed: {release_exc}",
            )
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
        storage.maybe_prune()
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
        "order.submitted",
        service_principal=principal.name,
        market=market.value,
        subject_id=order_id,
        detail=f"intent={order_intent.idempotency_key} "
               f"approval={order_intent.approval_id} "
               f"{order_intent.side} {order_intent.symbol} {order_intent.quantity}",
    )
    return {"order_id": order_id, "status": "SUBMITTED"}


@router.post("/markets/{market}/orders/{order_id}/cancel")
def cancel_order(market: Market, order_id: str,
                 principal: Principal = Depends(require_write)):
    require_cancel_service(principal, market)
    result = get_adapter(market).cancel_order(order_id)
    audit.record(
        "order.cancelled",
        service_principal=principal.name,
        market=market.value,
        subject_id=order_id,
    )
    return result
