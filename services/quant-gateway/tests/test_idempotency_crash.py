"""幂等崩溃窗口与多线程抢占测试。"""

import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from dsh_contracts import Market
from quant_gateway import approval_store, storage
from quant_gateway.adapters import register_adapter
from quant_gateway.adapters.paper import PaperAdapter
from quant_gateway.main import app
from quant_gateway.routers import orders as orders_router


@pytest.fixture(autouse=True)
def setup(monkeypatch, tmp_path):
    monkeypatch.setenv("QUANT_GATEWAY_DB", str(tmp_path / "gw.db"))
    monkeypatch.setenv("DSH_ENV", "development")
    monkeypatch.setenv("DSH_LOCAL_PAPER", "1")
    monkeypatch.setenv("PAPER_CRYPTO_ACCOUNT_ID", "paper-crypto-001")
    storage.reset()
    approval_store.reset()
    register_adapter(Market.CRYPTO, PaperAdapter(Market.CRYPTO))
    register_adapter(Market.A_SHARE, PaperAdapter(Market.A_SHARE))
    orders_router.check_order_risk = (
        lambda base_url, **payload: {"passed": True, "limits_hit": []}
    )
    yield
    storage.reset()
    approval_store.reset()


client = TestClient(app)


def _approve_and_snapshot(account="paper-crypto-001"):
    appr = client.post("/v1/approvals", json={
        "market": "CRYPTO",
        "requested_by_bot": "t",
        "subject_type": "order",
        "subject_id": "s1",
        "evidence_refs": ["a:1"],
    }).json()["approval_id"]
    client.post(f"/v1/approvals/{appr}/decide", json={
        "decision": "APPROVED", "decided_by": "alice",
    })
    client.post("/v1/markets/CRYPTO/risk-snapshots", json={
        "risk_snapshot_id": "rs-1",
        "market": "CRYPTO",
        "account_id": account,
        "position_before": "0",
        "position_after": "0.01",
        "risk_budget_delta": "1",
        "worst_case_loss": "0.01",
        "limits_hit": [],
        "as_of": "2026-01-01T00:00:00Z",
    })
    return appr


def _intent(appr, key="idem-1", account="paper-crypto-001"):
    return {
        "idempotency_key": key,
        "market": "CRYPTO",
        "account_id": account,
        "strategy_id": "s",
        "strategy_version": "1",
        "symbol": "BTCUSDT",
        "side": "BUY",
        "quantity": "0.01",
        "valid_until": "2099-01-01T00:00:00Z",
        "signal_snapshot_id": "sig",
        "risk_snapshot_id": "rs-1",
        "approval_id": appr,
    }


def test_concurrent_reserve_only_one_wins():
    appr = _approve_and_snapshot()
    intent = _intent(appr, key="idem-race")
    results = []

    def once():
        r = client.post("/v1/markets/CRYPTO/orders", json=intent)
        results.append(r.status_code)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: once(), range(8)))
    assert results.count(200) == 1
    assert results.count(409) == 7
    rec = storage.get_idempotency_record("idem-race")
    assert rec["status"] == "COMPLETED"
    assert rec["order_id"]


def test_crash_window_recover_via_paper_lookup():
    """模拟 RESERVED 且 venue 已下单、finalize 前崩溃：重放不重下单。"""
    appr = _approve_and_snapshot()
    intent = _intent(appr, key="idem-crash")
    # 手工：RESERVED + paper 订单已存在，order_id 未回填
    body = json.dumps(intent, sort_keys=True)
    h = hashlib.sha256(body.encode()).hexdigest()
    assert storage.record_idempotency_key("idem-crash", h)
    adapter = PaperAdapter(Market.CRYPTO)
    register_adapter(Market.CRYPTO, adapter)
    oid = adapter.request_order(intent)
    assert storage.get_idempotency_record("idem-crash")["order_id"] is None

    r = client.post("/v1/markets/CRYPTO/orders", json=intent)
    assert r.status_code == 409
    assert oid in r.json()["detail"]
    rec = storage.get_idempotency_record("idem-crash")
    assert rec["status"] == "COMPLETED"
    assert rec["order_id"] == oid


def test_begin_immediate_status_machine():
    assert storage.record_idempotency_key("k1", "hash-a")
    assert storage.get_idempotency_record("k1")["status"] == "RESERVED"
    storage.mark_idempotency_submitted("k1", "ord-1")
    assert storage.get_idempotency_record("k1")["status"] == "SUBMITTED"
    storage.finalize_idempotency_key("k1", "ord-1")
    assert storage.get_idempotency_record("k1")["status"] == "COMPLETED"
