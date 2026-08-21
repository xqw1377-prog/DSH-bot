"""审批存储与风控策略调用。

审批是资金动作的门禁：只有状态为 APPROVED 的审批才能放行订单。
审批账本持久化到 SQLite（见 storage.py），失败关闭：存储不可用时
抛异常，由调用方拒绝资金动作。

订单审批是一次性凭据：创建时必须携带意图绑定（binding），网关计算
intent_digest 并保存；下单时校验订单意图摘要与审批绑定一致，且原子地
消费审批（APPROVED -> CONSUMING -> CONSUMED）。一个审批只能放行一笔
订单，杜绝「一次批准、重复下单」。
"""

import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from uuid import uuid4

import httpx
from dsh_contracts import Approval, ApprovalStatus, Market

from quant_gateway import storage

RISK_POLICY_URL_DEFAULT = "http://127.0.0.1:8003"
# 审批有效期：超时未决的审批视为 EXPIRED，防止陈旧审批被滥用
APPROVAL_TTL = timedelta(minutes=30)

# 意图绑定字段：审批与订单必须逐字段一致，才允许消费该审批
_DIGEST_FIELDS = (
    "market",
    "account_id",
    "symbol",
    "side",
    "order_type",
    "quantity",
    "limit_price",
    "strategy_version",
    "signal_snapshot_id",
    "risk_snapshot_id",
    "valid_until",
)

_COLUMNS = "approval_id, status, market, requested_at, payload"


class ClaimStatus(StrEnum):
    """原子抢占审批 + 幂等键的结果。"""

    OK = "OK"
    APPROVAL_NOT_FOUND = "APPROVAL_NOT_FOUND"
    APPROVAL_NOT_APPROVED = "APPROVAL_NOT_APPROVED"
    APPROVAL_EXPIRED = "APPROVAL_EXPIRED"
    APPROVAL_INTENT_MISMATCH = "APPROVAL_INTENT_MISMATCH"
    APPROVAL_UNBOUNDED = "APPROVAL_UNBOUNDED"
    IDEMPOTENCY_IN_FLIGHT = "IDEMPOTENCY_IN_FLIGHT"


def _normalize_binding_field(field: str, value) -> str | None:
    if value is None or value == "":
        return None
    if field in ("quantity", "limit_price"):
        from decimal import Decimal, InvalidOperation
        try:
            return str(Decimal(str(value)))
        except InvalidOperation:
            return str(value)
    if field == "valid_until":
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return str(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")
    text = str(value).strip()
    if field in ("market", "side", "order_type"):
        return text.upper()
    return text


def compute_intent_digest(binding: dict) -> str:
    """从意图绑定字段计算规范化摘要（SHA-256）。

    字段缺失/空值不参与摘要，因此「A股市场不填 order_type」与
    「order_type 缺省」按同一意图处理；填写后必须精确一致。
    """
    canonical = {}
    for field in _DIGEST_FIELDS:
        normalized = _normalize_binding_field(field, binding.get(field))
        if normalized is not None:
            canonical[field] = normalized
    raw = json.dumps(canonical, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _row_to_approval(row) -> Approval:
    return Approval.model_validate_json(row[4])


def _save(approval: Approval) -> Approval:
    with storage.locked_conn() as conn:
        conn.execute(
            """INSERT INTO approvals (approval_id, status, market, requested_at, payload)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(approval_id) DO UPDATE SET
                   status = excluded.status,
                   payload = excluded.payload""",
            (
                approval.approval_id,
                approval.status.value,
                approval.market.value,
                approval.requested_at.isoformat(),
                approval.model_dump_json(),
            ),
        )
        conn.commit()
    return approval


def create_approval(
    market: Market,
    requested_by_bot: str,
    subject_type: str,
    subject_id: str,
    evidence_refs: list[str] | None = None,
    binding: dict | None = None,
) -> Approval:
    requested_at = datetime.now(UTC)
    intent_digest = compute_intent_digest(binding) if binding else None
    approval = Approval(
        approval_id=f"appr-{uuid4().hex[:12]}",
        status=ApprovalStatus.REQUESTED,
        market=market,
        requested_by_bot=requested_by_bot,
        requested_at=requested_at,
        subject_type=subject_type,
        subject_id=subject_id,
        evidence_refs=evidence_refs or [],
        intent_digest=intent_digest,
        expires_at=requested_at + APPROVAL_TTL,
    )
    return _save(approval)


def list_approvals(
    status: ApprovalStatus | None = None, market: Market | None = None
) -> list[Approval]:
    result = []
    with storage.locked_conn() as conn:
        rows = conn.execute(f"SELECT {_COLUMNS} FROM approvals").fetchall()
    for row in rows:
        approval = _row_to_approval(row)
        if _expired(approval):
            continue
        if status is not None and approval.status != status:
            continue
        if market is not None and approval.market != market:
            continue
        result.append(approval)
    return result


def get_approval(approval_id: str) -> Approval | None:
    with storage.locked_conn() as conn:
        row = conn.execute(
            f"SELECT {_COLUMNS} FROM approvals WHERE approval_id = ?", (approval_id,)
        ).fetchone()
    if row is None:
        return None
    approval = _row_to_approval(row)
    if _expired(approval):
        return None
    return approval


def decide_approval(
    approval_id: str, decision: ApprovalStatus, decided_by: str
) -> Approval:
    """仅允许 REQUESTED -> APPROVED / REJECTED，不可翻转已决审批。

    决定与消费（claim_order_reservation）一样必须在 BEGIN IMMEDIATE
    事务内完成，且 UPDATE 带 status='REQUESTED' 守卫：并发双击「批准」
    时只有一个赢家；已进入 CONSUMING/CONSUMED 的审批不可被改写，
    否则会抹掉 consumed_key 让审批复活（一次批准放行两笔订单）。
    """
    if decision not in (ApprovalStatus.APPROVED, ApprovalStatus.REJECTED):
        raise ValueError(f"invalid decision: {decision}")
    now = datetime.now(UTC)
    update = {
        "status": decision,
        "decided_by": decided_by,
        "decided_at": now,
    }
    if decision == ApprovalStatus.APPROVED:
        # 批准给出一段新鲜的有效窗口
        update["expires_at"] = now + APPROVAL_TTL
    with storage.locked_conn() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM approvals WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
            if row is None:
                conn.rollback()
                raise KeyError(approval_id)
            approval = _row_to_approval(row)
            if approval.status != ApprovalStatus.REQUESTED:
                conn.rollback()
                raise ValueError(f"approval already decided: {approval.status}")
            updated = approval.model_copy(update=update)
            cur = conn.execute(
                "UPDATE approvals SET status = ?, payload = ? "
                "WHERE approval_id = ? AND status = 'REQUESTED'",
                (updated.status.value, updated.model_dump_json(), approval_id),
            )
            if cur.rowcount != 1:
                # 并发赢家已改变状态：拒绝本次决定，不覆盖
                conn.rollback()
                raise ValueError(
                    f"approval state changed concurrently: {approval_id}"
                )
            conn.commit()
            return updated
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise


def _expired(approval: Approval) -> bool:
    if approval.status not in (ApprovalStatus.REQUESTED, ApprovalStatus.APPROVED):
        return False
    if approval.expires_at is None:
        return False
    try:
        expired = datetime.now(UTC) > approval.expires_at
    except TypeError:
        # 数据损坏：无法判定有效期，失败关闭视为已过期
        expired = True
    if expired:
        _save(approval.model_copy(update={"status": ApprovalStatus.EXPIRED}))
        return True
    return False


def claim_order_reservation(
    approval_id: str,
    intent_digest: str,
    idempotency_key: str,
    request_hash: str,
    now: datetime | None = None,
) -> tuple[ClaimStatus, str]:
    """原子抢占：审批消费 + 幂等键声明在同一 SQLite 事务。

    下单流程的唯一权威门禁：
    - 审批必须 APPROVED、未过期、intent_digest 与订单意图一致
    - 通过后审批进入 CONSUMING（记录消费所用幂等键与请求体哈希），
      同时幂等键抢占为 RESERVED，两者要么都成功要么都失败
    - 并发重复提交由唯一约束仲裁，只会有一个赢得消费
    """
    now = now or datetime.now(UTC)
    with storage.locked_conn() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM approvals WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
            if row is None:
                conn.rollback()
                return ClaimStatus.APPROVAL_NOT_FOUND, "approval not found"
            approval = _row_to_approval(row)
            if approval.status != ApprovalStatus.APPROVED:
                conn.rollback()
                return (
                    ClaimStatus.APPROVAL_NOT_APPROVED,
                    f"approval status is {approval.status.value}; not APPROVED",
                )
            try:
                expired = (
                    approval.expires_at is not None
                    and now > approval.expires_at
                )
            except TypeError:
                expired = True
            if expired:
                _mark_expired(conn, approval)
                conn.commit()
                return ClaimStatus.APPROVAL_EXPIRED, "approval expired"
            if approval.intent_digest is None:
                conn.rollback()
                return (
                    ClaimStatus.APPROVAL_UNBOUNDED,
                    "approval carries no intent binding; cannot authorize an order",
                )
            if approval.intent_digest != intent_digest:
                conn.rollback()
                return (
                    ClaimStatus.APPROVAL_INTENT_MISMATCH,
                    "order intent digest does not match approved binding",
                )
            idem_row = conn.execute(
                "SELECT request_hash, status FROM idempotency_keys WHERE key = ?",
                (idempotency_key,),
            ).fetchone()
            if idem_row is not None:
                prev_hash, status = idem_row
                if status == "FAILED" and prev_hash == request_hash:
                    conn.execute(
                        "DELETE FROM idempotency_keys WHERE key = ?",
                        (idempotency_key,),
                    )
                else:
                    conn.commit()
                    return (
                        ClaimStatus.IDEMPOTENCY_IN_FLIGHT,
                        "idempotency key already claimed by another request",
                    )
            conn.execute(
                "INSERT INTO idempotency_keys (key, request_hash, status) "
                "VALUES (?, ?, 'RESERVED')",
                (idempotency_key, request_hash),
            )
            consumed = approval.model_copy(update={
                "status": ApprovalStatus.CONSUMING,
                "consumed_key": idempotency_key,
                "consumed_request_hash": request_hash,
                "consumed_at": now,
            })
            conn.execute(
                "UPDATE approvals SET status = ?, payload = ? WHERE approval_id = ?",
                (consumed.status.value, consumed.model_dump_json(), approval_id),
            )
            conn.commit()
            return ClaimStatus.OK, "approval consumed; order authorized"
        except sqlite3.IntegrityError:
            conn.rollback()
            return (
                ClaimStatus.IDEMPOTENCY_IN_FLIGHT,
                "idempotency key already claimed; concurrent request won",
            )
        except Exception:
            conn.rollback()
            raise


def _mark_expired(conn, approval: Approval) -> None:
    updated = approval.model_copy(update={"status": ApprovalStatus.EXPIRED})
    conn.execute(
        "UPDATE approvals SET status = ?, payload = ? WHERE approval_id = ?",
        (updated.status.value, updated.model_dump_json(), approval.approval_id),
    )


def release_consumed_approval(conn, idempotency_key: str) -> None:
    """把已消费的审批释放回 APPROVED（幂等键失败后允许按新单重试）。

    与幂等键 FAILED 标记同一事务调用（见 storage.mark_idempotency_failed）。
    """
    storage.release_consumed_approval(conn, idempotency_key)


def finalize_consumed_approval(conn, idempotency_key: str, order_id: str) -> None:
    """消费完成：审批进入 CONSUMED 并回填权威订单 ID。

    与幂等键 COMPLETED 标记同一事务调用（见 storage.finalize_idempotency_key）。
    """
    storage.finalize_consumed_approval(conn, idempotency_key, order_id)


def reset() -> None:
    """测试辅助：清空审批账本。"""
    storage.reset()


# ---- 二次硬风控：调用 risk-policy，失败关闭 ----

def check_order_risk(base_url: str | None = None, **payload) -> dict:
    """调用 risk-policy /v1/check-order。

    任何网络或上游错误都必须抛出异常，由调用方拒绝订单（失败关闭），
    绝不返回「通过」的猜测结果。
    """
    url = (base_url or RISK_POLICY_URL_DEFAULT).rstrip("/") + "/v1/check-order"
    resp = httpx.post(url, json=payload, timeout=3.0)
    resp.raise_for_status()
    return resp.json()
