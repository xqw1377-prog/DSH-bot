"""审计日志：所有资金相关动作必须可追溯。

记录审批创建/决定、订单提交/撤销、策略控制与紧急停止，
与业务数据同库持久化，重启不丢。
"""

from datetime import UTC, datetime
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException

from quant_gateway import storage
from quant_gateway.auth import require_read

router = APIRouter()


def record(
    action: str,
    service_principal: str,
    actor_principal: str | None = None,
    market: str | None = None,
    subject_id: str | None = None,
    outcome: str = "OK",
    detail: str | None = None,
) -> None:
    with storage.locked_conn() as conn:
        conn.execute(
            """INSERT INTO audit_log
           (
               audit_id, occurred_at, service_principal, actor_principal,
               action, market, subject_id, outcome, detail
           )
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            f"audit-{uuid4().hex[:12]}",
            datetime.now(UTC).isoformat(),
            service_principal,
            actor_principal,
            action,
            market,
            subject_id,
            outcome,
            detail,
        ),
        )
        conn.commit()


@router.get("/audit", dependencies=[Depends(require_read)])
def list_audit(limit: int = 100, actor: str | None = None):
    """审计日志只读查询，必须携带 read 权限。"""
    if not (1 <= limit <= 1000):
        raise HTTPException(status_code=422, detail="limit must be within 1..1000")
    sql = (
        "SELECT audit_id, occurred_at, service_principal, actor_principal, "
        "action, market, subject_id, outcome, detail FROM audit_log"
    )
    params: list = []
    if actor:
        sql += " WHERE COALESCE(actor_principal, service_principal) = ?"
        params.append(actor)
    sql += " ORDER BY occurred_at DESC LIMIT ?"
    params.append(limit)
    with storage.locked_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    keys = (
        "audit_id",
        "occurred_at",
        "service_principal",
        "actor_principal",
        "action",
        "market",
        "subject_id",
        "outcome",
        "detail",
    )
    return [dict(zip(keys, r, strict=False)) for r in rows]
