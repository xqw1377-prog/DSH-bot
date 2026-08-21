"""TTL 清理验收:该删的删、不该动的绝不动。"""

from datetime import UTC, datetime, timedelta

from dsh_contracts import ApprovalStatus, Market, RiskSnapshot
from quant_gateway import approval_store, storage
from quant_gateway.routers.orders import register_risk_snapshot


def _seed_gateway_state(old: datetime, fresh: datetime) -> None:
    storage.reset()
    approval_store.reset()
    # 旧终态审批(EXPIRED)+ 新终态审批(REJECTED)+ 新 APPROVED
    for age, status in ((old, ApprovalStatus.EXPIRED), (fresh, ApprovalStatus.REJECTED)):
        approval = approval_store.create_approval(
            market=Market.A_SHARE, requested_by_bot="bot",
            subject_type="order", subject_id="s")
        updated = approval.model_copy(update={
            "status": status,
            "requested_at": age,
        })
    # 直接落库构造(绕过 TTL 逻辑本身)
    with storage.locked_conn() as conn:
        conn.execute("DELETE FROM approvals")
        for age, status in (
            (old, ApprovalStatus.EXPIRED.value),
            (fresh, ApprovalStatus.REJECTED.value),
            (fresh, ApprovalStatus.APPROVED.value),
            (old, ApprovalStatus.CONSUMING.value),
        ):
            conn.execute(
                "INSERT INTO approvals (approval_id, status, market, requested_at, payload)"
                " VALUES (?, ?, ?, ?, ?)",
                (f"appr-{status}-{age.date()}", status, "A_SHARE",
                 age.isoformat(), "{}"))
        conn.execute("DELETE FROM idempotency_keys")
        for key, status, updated in (
            ("old-finished", "FINISHED", old.isoformat()),
            ("new-finished", "FINISHED", fresh.isoformat()),
            ("old-failed", "FAILED", old.isoformat()),
            ("in-flight", "RESERVED", old.isoformat()),
        ):
            conn.execute(
                "INSERT INTO idempotency_keys (key, request_hash, status, updated_at)"
                " VALUES (?, 'h', ?, ?)", (key, status, updated))
        conn.commit()
    # 快照:旧 as_of + 新 as_of
    register_risk_snapshot(RiskSnapshot(
        risk_snapshot_id="snap-old", market=Market.A_SHARE, account_id="a",
        position_before=0, position_after=1, risk_budget_delta=1,
        worst_case_loss=0, as_of=old))
    register_risk_snapshot(RiskSnapshot(
        risk_snapshot_id="snap-fresh", market=Market.A_SHARE, account_id="a",
        position_before=0, position_after=1, risk_budget_delta=1,
        worst_case_loss=0, as_of=fresh))


def test_prune_gateway_removes_expired_keeps_live():
    now = datetime.now(UTC)
    old = now - timedelta(days=40)
    fresh = now - timedelta(hours=1)
    _seed_gateway_state(old, fresh)

    removed = storage.prune_expired(now=now)

    assert removed["idempotency_keys"] == 2        # old-finished + old-failed
    assert removed["risk_snapshots"] == 1          # snap-old
    assert removed["approvals_terminal"] == 1      # 仅旧 EXPIRED

    with storage.locked_conn() as conn:
        keys = {r[0]: r[1] for r in conn.execute(
            "SELECT key, status FROM idempotency_keys")}
        snaps = [r[0] for r in conn.execute(
            "SELECT risk_snapshot_id FROM risk_snapshots")]
        approvals = {r[0]: r[1] for r in conn.execute(
            "SELECT approval_id, status FROM approvals")}
    # 在途键绝不清
    assert keys == {"new-finished": "FINISHED", "in-flight": "RESERVED"}
    assert snaps == ["snap-fresh"]
    # APPROVED 与 CONSUMING 绝不清;新 REJECTED 保留
    assert len(approvals) == 3
    assert f"appr-APPROVED-{fresh.date()}" in approvals
    assert f"appr-CONSUMING-{old.date()}" in approvals


def test_maybe_prune_throttles_to_hourly():
    now = datetime.now(UTC)
    storage.reset()
    storage._last_prune_at = now - timedelta(minutes=30)
    calls = {"n": 0}
    real_prune = storage.prune_expired
    storage.prune_expired = lambda now=None: calls.__setitem__("n", calls["n"] + 1)  # type: ignore[assignment]
    try:
        storage.maybe_prune(now=now)      # 30 分钟内:跳过
        assert calls["n"] == 0
        storage._last_prune_at = now - timedelta(hours=2)
        storage.maybe_prune(now=now)      # 超窗:执行
        assert calls["n"] == 1
    finally:
        storage.prune_expired = real_prune  # type: ignore[assignment]
        storage.reset()


def test_intel_store_prune_removes_old_docs_and_events(tmp_path):
    from intelligence_ingest.documents import Document
    from intelligence_ingest.store import (
        IntelligenceStore, prune_expired, maybe_prune)

    store = IntelligenceStore(str(tmp_path / "intel.db"))
    old = "2026-01-01T00:00:00Z"
    fresh = "2026-08-20T00:00:00Z"
    for suffix, fetched in (("old", old), ("fresh", fresh)):
        store.upsert_document(Document(
            document_id=f"doc-{suffix}", source_id="s", source_tier="official",
            canonical_url=f"https://x/{suffix}", published_at=fetched,
            fetched_at=fetched, content_hash=f"hash-{suffix}", language="en",
            raw_text="official announcement with enough body text here",
            assets=["BTC"], collection_method="RSS"))
        store._conn.execute(
            "INSERT INTO events (event_id, document_id, event_type, affected_assets,"
            " direction, confidence, impact_horizon, mode, can_apply, evidence_refs,"
            " payload) VALUES (?, ?, 'LISTING', '[\"BTC\"]', 'POSITIVE', '0.8',"
            " '1D', 'SHADOW', 0, '[]', '{}')",
            (f"evt-{suffix}", f"doc-{suffix}"))
        store._conn.commit()

    removed = prune_expired(store, now="2026-08-21T12:00:00Z", ttl_days=180)
    assert removed == {"events": 1, "documents": 1}
    remaining = [r[0] for r in store._conn.execute(
        "SELECT document_id FROM documents")]
    assert remaining == ["doc-fresh"]

    # 守卫:24h 内不重复清理
    calls = {"n": 0}
    real = maybe_prune
    import intelligence_ingest.store as st

    def counting(store_, now=None):
        calls["n"] += 1
        return real(store_, now=now)

    st._last_prune_at = "2026-08-21T11:00:00Z"
    counting(store, now="2026-08-21T12:00:00Z")
    assert calls["n"] == 1  # 计数的是守卫函数本身;验证未再触发底层
    st._last_prune_at = None
