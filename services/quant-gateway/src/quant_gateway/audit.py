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


GENESIS_HASH = "0" * 64

_CHAIN_FIELDS = (
    "audit_id, occurred_at, service_principal, actor_principal, "
    "action, market, subject_id, outcome, detail"
)


def _entry_hash(prev_hash: str, row: dict) -> str:
    """哈希链:entry_hash = sha256(prev_hash + 规范化行内容)。

    任何对历史行的改动都会使其后所有 entry_hash 校验失败。
    """
    import hashlib

    canonical = "|".join(
        str(row.get(field) or "") for field in (
            "audit_id", "occurred_at", "service_principal", "actor_principal",
            "action", "market", "subject_id", "outcome", "detail",
        )
    )
    return hashlib.sha256(
        (prev_hash + "|" + canonical).encode("utf-8")).hexdigest()


def record(
    action: str,
    service_principal: str,
    actor_principal: str | None = None,
    market: str | None = None,
    subject_id: str | None = None,
    outcome: str = "OK",
    detail: str | None = None,
) -> None:
    """追加审计行并接入哈希链。

    链写入与行插入同事务:读链尾 → 计算哈希 → 插入。多 worker 并发
    由 SQLite 写锁串行化,链不会分叉。
    """
    with storage.locked_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        tail = conn.execute(
            "SELECT entry_hash FROM audit_log"
            " WHERE entry_hash IS NOT NULL ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        prev_hash = tail[0] if tail else GENESIS_HASH
        row = {
            "audit_id": f"audit-{uuid4().hex[:12]}",
            "occurred_at": datetime.now(UTC).isoformat(),
            "service_principal": service_principal,
            "actor_principal": actor_principal,
            "action": action,
            "market": market,
            "subject_id": subject_id,
            "outcome": outcome,
            "detail": detail,
        }
        entry_hash = _entry_hash(prev_hash, row)
        conn.execute(
            """INSERT INTO audit_log
           (
               audit_id, occurred_at, service_principal, actor_principal,
               action, market, subject_id, outcome, detail,
               prev_hash, entry_hash
           )
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            row["audit_id"], row["occurred_at"], row["service_principal"],
            row["actor_principal"], row["action"], row["market"],
            row["subject_id"], row["outcome"], row["detail"],
            prev_hash, entry_hash,
        ),
        )
        conn.commit()


def verify_chain(limit: int | None = None) -> dict:
    """校验哈希链完整性。

    返回 {ok, chained_rows, legacy_rows, first_broken_audit_id}。
    legacy_rows 是建链之前的历史行(无哈希),不参与校验但被计数。
    任何被篡改/删除的链上行都会使 ok=False。
    """
    sql = (
        f"SELECT rowid, {_CHAIN_FIELDS}, prev_hash, entry_hash "
        "FROM audit_log ORDER BY rowid"
    )
    with storage.locked_conn() as conn:
        rows = [dict(zip(
            ("rowid", "audit_id", "occurred_at", "service_principal",
             "actor_principal", "action", "market", "subject_id",
             "outcome", "detail", "prev_hash", "entry_hash"),
            r, strict=False)) for r in conn.execute(sql)]
    if limit is not None:
        rows = rows[-limit:]
    expected_prev = None
    chained = 0
    legacy = 0
    for row in rows:
        if row["entry_hash"] is None:
            legacy += 1
            continue
        if expected_prev is None:
            expected_prev = row["prev_hash"]
        if row["prev_hash"] != expected_prev:
            return {"ok": False, "chained_rows": chained,
                    "legacy_rows": legacy,
                    "first_broken_audit_id": row["audit_id"]}
        if _entry_hash(row["prev_hash"], row) != row["entry_hash"]:
            return {"ok": False, "chained_rows": chained,
                    "legacy_rows": legacy,
                    "first_broken_audit_id": row["audit_id"]}
        expected_prev = row["entry_hash"]
        chained += 1
    return {"ok": True, "chained_rows": chained, "legacy_rows": legacy,
            "first_broken_audit_id": None}


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
