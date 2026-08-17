from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from dsh_contracts import (
    AccountSummary,
    ApprovalStatus,
    Market,
    RiskSnapshot,
)
from fastapi.testclient import TestClient

from quant_gateway import approval_store
from quant_gateway.main import app
from quant_gateway.routers import orders as orders_router
from quant_gateway.routers.orders import register_risk_snapshot


@pytest.fixture()
def client():
    return TestClient(app)


@pytest.fixture()
def risk_pass(monkeypatch):
    """让二次硬风控直接通过（risk-policy 的规则另由其自身测试覆盖）。"""
    monkeypatch.setattr(
        orders_router, "check_order_risk",
        lambda base_url, **payload: {"passed": True, "limits_hit": []},
    )


@pytest.fixture()
def risk_reject(monkeypatch):
    monkeypatch.setattr(
        orders_router, "check_order_risk",
        lambda base_url, **payload: {"passed": False, "limits_hit": ["max_position"]},
    )


def approved_approval() -> str:
    approval = approval_store.create_approval(
        market=Market.A_SHARE,
        requested_by_bot="a-stock-bot",
        subject_type="order",
        subject_id="sub-1",
    )
    return approval_store.decide_approval(
        approval.approval_id, ApprovalStatus.APPROVED, "human"
    ).approval_id


def make_intent(**overrides) -> dict:
    intent = {
        "idempotency_key": "key-1",
        "market": "A_SHARE",
        "account_id": "acc-1",
        "strategy_id": "strat-1",
        "strategy_version": "0.1.0",
        "symbol": "600519.SH",
        "side": "BUY",
        "quantity": "100",
        "valid_until": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
        "signal_snapshot_id": "sig-1",
        "risk_snapshot_id": "risk-1",
        "approval_id": "appr-1",
    }
    intent.update(overrides)
    return intent


def make_snapshot(**overrides) -> RiskSnapshot:
    base = dict(
        risk_snapshot_id="risk-1",
        market=Market.A_SHARE,
        account_id="acc-1",
        position_before=Decimal("0"),
        position_after=Decimal("100"),
        risk_budget_delta=Decimal("10000"),
        worst_case_loss=Decimal("500"),
        as_of=datetime.now(UTC),
    )
    base.update(overrides)
    return RiskSnapshot(**base)


def error_body(resp) -> dict:
    detail = resp.json()["detail"]
    if isinstance(detail, dict):
        return detail
    return {"message": str(detail)}


def test_order_rejected_without_risk_snapshot(client, risk_pass):
    resp = client.post("/v1/markets/A_SHARE/orders", json=make_intent())
    assert resp.status_code == 422
    body = error_body(resp)
    assert "fail-closed" in body["message"]
    assert body["phase"] == "PRE_SUBMIT"
    assert body["submission_unknown"] is False


def test_order_rejected_when_limits_hit(client, risk_pass):
    register_risk_snapshot(make_snapshot(limits_hit=["max_position"]))
    resp = client.post("/v1/markets/A_SHARE/orders", json=make_intent())
    assert resp.status_code == 422


def test_kill_switch_blocks_new_orders_until_resume(client, risk_pass):
    register_risk_snapshot(make_snapshot())
    stopped = client.post("/v1/markets/A_SHARE/emergency-stop")
    assert stopped.status_code == 200
    health = client.get("/v1/markets/A_SHARE/health").json()
    assert health["system_ok"] is False
    assert health["trading_channel_ok"] is False

    blocked = client.post(
        "/v1/markets/A_SHARE/orders",
        json=make_intent(approval_id=approved_approval()),
    )
    assert blocked.status_code == 409
    body = error_body(blocked)
    assert body["error_code"] == "TRADING_HALTED"
    assert body["phase"] == "PRE_SUBMIT"
    assert body["submission_unknown"] is False

    resumed = client.post("/v1/markets/A_SHARE/kill-switch/resume")
    assert resumed.status_code == 200
    health = client.get("/v1/markets/A_SHARE/health").json()
    assert health["system_ok"] is True
    assert health["trading_channel_ok"] is True

    ok = client.post(
        "/v1/markets/A_SHARE/orders",
        json=make_intent(
            idempotency_key="after-resume",
            approval_id=approved_approval(),
        ),
    )
    assert ok.status_code == 200
    assert ok.json()["status"] == "SUBMITTED"


def test_critical_risk_engages_kill_switch(client, monkeypatch):
    from quant_gateway.adapters import get_adapter

    monkeypatch.setattr(
        orders_router, "check_order_risk",
        lambda base_url, **payload: {
            "passed": False,
            "limits_hit": ["equity_unavailable"],
            "severity": "CRITICAL",
            "kill_switch": True,
        },
    )
    register_risk_snapshot(make_snapshot())
    resp = client.post("/v1/markets/A_SHARE/orders", json=make_intent())
    assert resp.status_code == 422
    adapter = get_adapter(Market.A_SHARE)
    assert adapter.stopped is True
    audit = client.get("/v1/audit").json()
    assert any(e["action"] == "kill_switch.requested" for e in audit)
    assert any(e["action"] == "kill_switch.succeeded" for e in audit)


def test_order_rejected_when_risk_policy_rejects(client, risk_reject):
    register_risk_snapshot(make_snapshot())
    resp = client.post("/v1/markets/A_SHARE/orders", json=make_intent())
    assert resp.status_code == 422
    body = error_body(resp)
    assert "risk check failed" in body["message"]
    assert body["phase"] == "PRE_SUBMIT"
    assert body["submission_unknown"] is False


def test_order_rejected_when_risk_policy_unreachable(client, monkeypatch):
    def unreachable(*args, **kwargs):
        raise ConnectionError("risk-policy down")

    monkeypatch.setattr(orders_router, "check_order_risk", unreachable)
    register_risk_snapshot(make_snapshot())
    resp = client.post("/v1/markets/A_SHARE/orders", json=make_intent())
    assert resp.status_code == 503
    body = error_body(resp)
    assert body["error_code"] == "RISK_POLICY_UNAVAILABLE"
    assert body["phase"] == "PRE_SUBMIT"
    assert body["retryable"] is True
    assert body["submission_unknown"] is False
    assert "fail-closed" in body["message"]


def test_order_rejected_without_approved_approval(client, risk_pass):
    register_risk_snapshot(make_snapshot())
    pending = approval_store.create_approval(
        market=Market.A_SHARE,
        requested_by_bot="a-stock-bot",
        subject_type="order",
        subject_id="sub-1",
    ).approval_id
    resp = client.post(
        "/v1/markets/A_SHARE/orders", json=make_intent(approval_id=pending)
    )
    assert resp.status_code == 422


def test_order_submitted_when_all_gates_pass(client, risk_pass):
    register_risk_snapshot(make_snapshot())
    resp = client.post(
        "/v1/markets/A_SHARE/orders",
        json=make_intent(approval_id=approved_approval()),
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "SUBMITTED"


def test_idempotency_replay_rejected(client, risk_pass):
    register_risk_snapshot(make_snapshot())
    body = make_intent(idempotency_key="dup-key", approval_id=approved_approval())
    assert client.post("/v1/markets/A_SHARE/orders", json=body).status_code == 200
    resp = client.post("/v1/markets/A_SHARE/orders", json=body)
    assert resp.status_code == 409


def test_intent_validation_rejects_fuzzy_body(client, risk_pass):
    register_risk_snapshot(make_snapshot())
    resp = client.post(
        "/v1/markets/A_SHARE/orders",
        json=make_intent(idempotency_key="bad-1", quantity=None),
    )
    assert resp.status_code == 422


def test_same_key_different_body_conflicts(client, risk_pass):
    register_risk_snapshot(make_snapshot())
    approval_id = approved_approval()
    first = client.post(
        "/v1/markets/A_SHARE/orders",
        json=make_intent(idempotency_key="clash-key", approval_id=approval_id),
    )
    assert first.status_code == 200
    # 同一幂等键但请求体不同（数量改变）→ 冲突拒绝
    resp = client.post(
        "/v1/markets/A_SHARE/orders",
        json=make_intent(
            idempotency_key="clash-key", quantity="200", approval_id=approval_id
        ),
    )
    assert resp.status_code == 409
    assert "different request body" in error_body(resp)["message"]
    assert error_body(resp)["phase"] == "PRE_SUBMIT"
    assert error_body(resp)["submission_unknown"] is False


def test_concurrent_duplicate_requests_submit_exactly_once(client, risk_pass):
    """并发原子性：两个线程同时提交相同幂等键，只允许一笔订单。"""
    import threading

    from quant_gateway.adapters import get_adapter

    register_risk_snapshot(make_snapshot())
    body = make_intent(idempotency_key="race-key", approval_id=approved_approval())

    barrier = threading.Barrier(4)

    def slow_submit():
        barrier.wait()  # 尽量同时进入
        return client.post("/v1/markets/A_SHARE/orders", json=body)

    threads_results: list = []
    lock = threading.Lock()

    def worker():
        resp = slow_submit()
        with lock:
            if resp.status_code == 200:
                # 成功响应必须全部指向同一订单（含崩溃恢复认领路径）
                threads_results.append(resp.json()["order_id"])
            else:
                threads_results.append(str(resp.status_code))

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    successes = [r for r in threads_results if not r.isdigit()]
    assert 1 <= len(successes) <= 2  # 正常并发 1 个；恢复竞态下最多 2 个
    assert len(set(successes)) == 1  # 但必须共享同一 order_id
    adapter = get_adapter(Market.A_SHARE)
    assert len(adapter.submitted) == 1  # 量化系统只收到一笔


def test_crash_after_venue_accept_recovers_same_order(risk_pass, monkeypatch):
    """venue 已接单、网关 finalize 前崩溃：重试必须认领同一订单，不得重复下单。"""
    from quant_gateway import storage as gw_storage

    # 不把服务端异常抛进测试进程，拿到真实 500 响应
    client = TestClient(app, raise_server_exceptions=False)

    register_risk_snapshot(make_snapshot())
    body = make_intent(idempotency_key="crash-key", approval_id=approved_approval())

    # 模拟崩溃：finalize 抛异常（venue 已接受，幂等键停留在 RESERVED）
    real_finalize = gw_storage.finalize_idempotency_key

    def crashing_finalize(key, order_id):
        raise RuntimeError("process crashed before finalize")

    monkeypatch.setattr(orders_router.storage, "finalize_idempotency_key",
                        crashing_finalize)
    first = client.post("/v1/markets/A_SHARE/orders", json=body)
    assert first.status_code == 503
    assert error_body(first)["error_code"] == "LOCAL_PERSIST_FAILED"
    assert error_body(first)["submission_unknown"] is True
    monkeypatch.setattr(orders_router.storage, "finalize_idempotency_key",
                        real_finalize)

    # 宽限窗口内重试：在途 409，禁止恢复判定
    early = client.post("/v1/markets/A_SHARE/orders", json=body)
    assert early.status_code == 409
    assert "in flight" in error_body(early)["message"]
    assert error_body(early)["submission_unknown"] is True

    # 老化幂等键（越过宽限窗口）
    with gw_storage.locked_conn() as conn:
        conn.execute(
            "UPDATE idempotency_keys SET updated_at = ? WHERE key = ?",
            ("2020-01-01T00:00:00Z", "crash-key"),
        )
        conn.commit()

    recovered = client.post("/v1/markets/A_SHARE/orders", json=body)
    assert recovered.status_code == 200
    assert recovered.json()["recovered"] is True
    assert recovered.json()["order_id"] == "A_SHARE-ord-1"

    from quant_gateway.adapters import get_adapter
    adapter = get_adapter(Market.A_SHARE)
    assert len(adapter.submitted) == 1  # venue 只收到过一笔

    # 再重试：普通幂等重放 409，指向已认领订单
    again = client.post("/v1/markets/A_SHARE/orders", json=body)
    assert again.status_code == 409
    assert "A_SHARE-ord-1" in error_body(again)["message"]

    audit = client.get("/v1/audit").json()
    assert any(e["action"] == "order.submission_recovered" for e in audit)


def test_eventual_consistency_adapter_never_auto_releases(client, risk_pass):
    """弱一致查询的适配器：查无不能断定未接单，禁止自动释放重试。"""
    from quant_gateway.adapters import get_adapter

    register_risk_snapshot(make_snapshot())
    body = make_intent(idempotency_key="eventual-key",
                       approval_id=approved_approval())
    adapter = get_adapter(Market.A_SHARE)
    adapter.order_lookup_consistency = "EVENTUAL"

    from quant_gateway import storage as gw_storage
    import hashlib as _h
    import json as _json
    from dsh_contracts import OrderIntent as _OI
    canonical = _OI.model_validate(body).model_dump(mode="json")
    real_hash = _h.sha256(_json.dumps(
        canonical, sort_keys=True).encode()).hexdigest()
    assert gw_storage.record_idempotency_key("eventual-key", real_hash)
    with gw_storage.locked_conn() as conn:
        conn.execute(
            "UPDATE idempotency_keys SET updated_at = '2020-01-01T00:00:00Z'"
            " WHERE key = 'eventual-key'"
        )
        conn.commit()
    resp = client.post("/v1/markets/A_SHARE/orders", json=body)
    assert resp.status_code == 409
    assert "not strongly consistent" in error_body(resp)["message"]
    assert error_body(resp)["submission_unknown"] is True
    record = gw_storage.get_idempotency_record("eventual-key")
    assert record["status"] != "FAILED"
