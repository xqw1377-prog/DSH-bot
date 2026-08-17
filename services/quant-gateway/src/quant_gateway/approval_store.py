"""审批存储与风控策略调用。

审批是资金动作的门禁：只有状态为 APPROVED 的审批才能放行订单。
审批账本持久化到 SQLite（见 storage.py），失败关闭：存储不可用时
抛异常，由调用方拒绝资金动作。
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
from dsh_contracts import Approval, ApprovalStatus, Market

from quant_gateway import storage

RISK_POLICY_URL_DEFAULT = "http://127.0.0.1:8003"
# 审批有效期：超时未决的审批视为 EXPIRED，防止陈旧审批被滥用
APPROVAL_TTL = timedelta(minutes=30)

_COLUMNS = "approval_id, status, market, requested_at, payload"


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
) -> Approval:
    approval = Approval(
        approval_id=f"appr-{uuid4().hex[:12]}",
        status=ApprovalStatus.REQUESTED,
        market=market,
        requested_by_bot=requested_by_bot,
        requested_at=datetime.now(UTC),
        subject_type=subject_type,
        subject_id=subject_id,
        evidence_refs=evidence_refs or [],
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
        # 触发过期检查（会更新状态为 EXPIRED）
        _expired(approval)
        # 重新读取更新后的状态
        with storage.locked_conn() as conn:
            updated_row = conn.execute(
                f"SELECT {_COLUMNS} FROM approvals WHERE approval_id = ?",
                (approval.approval_id,),
            ).fetchone()
        if updated_row is None:
            continue
        approval = _row_to_approval(updated_row)
        if status is not None and approval.status != status:
            continue
        if market is not None and approval.market != market:
            continue
        result.append(approval)
    return result


def get_approval(approval_id: str) -> Approval | None:
    """获取审批。返回 None 表示审批不存在（404）。

    过期的审批不返回 None，而是返回 status=EXPIRED 的审批对象，
    让调用方能区分「审批不存在」与「审批已过期」。
    """
    with storage.locked_conn() as conn:
        row = conn.execute(
            f"SELECT {_COLUMNS} FROM approvals WHERE approval_id = ?", (approval_id,)
        ).fetchone()
    if row is None:
        return None
    approval = _row_to_approval(row)
    # 过期审批：保存 EXPIRED 状态并返回，不返回 None
    _expired(approval)
    # 重新读取（_expired 可能已更新状态）
    with storage.locked_conn() as conn:
        row = conn.execute(
            f"SELECT {_COLUMNS} FROM approvals WHERE approval_id = ?", (approval_id,)
        ).fetchone()
    return _row_to_approval(row) if row else approval


def decide_approval(
    approval_id: str, decision: ApprovalStatus, decided_by: str
) -> Approval:
    """仅允许 REQUESTED -> APPROVED / REJECTED，不可翻转已决审批。"""
    approval = get_approval(approval_id)
    if approval is None:
        raise KeyError(approval_id)
    if approval.status != ApprovalStatus.REQUESTED:
        raise ValueError(f"approval already decided: {approval.status}")
    if decision not in (ApprovalStatus.APPROVED, ApprovalStatus.REJECTED):
        raise ValueError(f"invalid decision: {decision}")
    return _save(approval.model_copy(update={
        "status": decision,
        "decided_by": decided_by,
        "decided_at": datetime.now(UTC),
    }))


def is_approved(approval_id: str) -> bool:
    approval = get_approval(approval_id)
    return approval is not None and approval.status == ApprovalStatus.APPROVED


def _expired(approval: Approval) -> bool:
    if approval.status == ApprovalStatus.REQUESTED:
        if datetime.now(UTC) - approval.requested_at > APPROVAL_TTL:
            _save(approval.model_copy(update={"status": ApprovalStatus.EXPIRED}))
            return True
    return False


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
