from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from dsh_contracts import (
    AccountSummary,
    Approval,
    ApprovalStatus,
    Market,
    OrderIntent,
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


def make_binding(**overrides) -> dict:
    """订单意图绑定：审批与订单必须逐字段一致。

    valid_until 用固定未来时间，保证审批绑定与下单意图摘要一致。
    """
    binding = {
        "market": "A_SHARE",
        "account_id": "acc-1",
        "symbol": "600519.SH",
        "side": "BUY",
        "order_type": None,
        "quantity": "100",
        "limit_price": None,
        "strategy_version": "0.1.0",
        "signal_snapshot_id": "sig-1",
        "risk_snapshot_id": "risk-1",
        "valid_until": "2099-01-01T00:00:00Z",
    }
    binding.update(overrides)
    return binding


def approved_approval(binding: dict | None = None) -> str:
    approval = approval_store.create_approval(
        market=Market.A_SHARE,
        requested_by_bot="a-stock-bot",
        subject_type="order",
        subject_id="sub-1",
        binding=binding if binding is not None else make_binding(),
    )
    return approval_store.decide_approval(
        approval.approval_id, ApprovalStatus.APPROVED, "human"
    ).approval_id


def make_intent(**overrides) -> dict:
    binding = make_binding()
    intent = {
        "idempotency_key": "key-1",
        "market": "A_SHARE",
        "account_id": "acc-1",
        "strategy_id": "strat-1",
        "strategy_version": binding["strategy_version"],
        "symbol": binding["symbol"],
        "side": binding["side"],
        "quantity": binding["quantity"],
        "valid_until": binding["valid_until"],
        "signal_snapshot_id": binding["signal_snapshot_id"],
        "risk_snapshot_id": binding["risk_snapshot_id"],
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


# ---- 审批一次性订单凭据（P0-1 验收）----

def test_approval_binds_order_intent(client, risk_pass):
    """审批绑定意图：改数量即意图变更，必须拒绝；匹配意图放行。"""
    from quant_gateway.adapters import get_adapter

    register_risk_snapshot(make_snapshot())
    approval_id = approved_approval()  # binding: quantity=100, side=BUY, symbol=600519.SH
    mismatch = client.post(
        "/v1/markets/A_SHARE/orders",
        json=make_intent(approval_id=approval_id, quantity="200"),
    )
    assert mismatch.status_code == 409
    assert error_body(mismatch)["error_code"] == "APPROVAL_INTENT_MISMATCH"
    assert error_body(mismatch)["phase"] == "PRE_SUBMIT"
    assert get_adapter(Market.A_SHARE).submitted == []

    ok = client.post(
        "/v1/markets/A_SHARE/orders",
        json=make_intent(idempotency_key="match-after", approval_id=approval_id),
    )
    assert ok.status_code == 200


def test_expired_approval_rejected(client, risk_pass):
    """TTL 过期：已批准的审批到期后不得放行订单。"""
    register_risk_snapshot(make_snapshot())
    approval_id = approved_approval()
    with approval_store.storage.locked_conn() as conn:
        row = conn.execute(
            "SELECT payload FROM approvals WHERE approval_id = ?", (approval_id,)
        ).fetchone()
        approval = Approval.model_validate_json(row[0])
        aged = approval.model_copy(update={
            "expires_at": datetime.now(UTC) - timedelta(seconds=1),
        })
        conn.execute(
            "UPDATE approvals SET payload = ? WHERE approval_id = ?",
            (aged.model_dump_json(), approval_id),
        )
        conn.commit()
    resp = client.post(
        "/v1/markets/A_SHARE/orders",
        json=make_intent(idempotency_key="expired-1", approval_id=approval_id),
    )
    assert resp.status_code == 422
    assert error_body(resp)["error_code"] == "APPROVAL_NOT_APPROVED"
    stored = approval_store.get_approval(approval_id)
    assert stored is not None and stored.status == ApprovalStatus.EXPIRED


def test_approval_is_one_time_credential(client, risk_pass):
    """一次性凭据：一次批准只放行一笔订单，不同幂等键重放必须拒绝。"""
    from quant_gateway.adapters import get_adapter

    register_risk_snapshot(make_snapshot())
    approval_id = approved_approval()
    first = client.post(
        "/v1/markets/A_SHARE/orders",
        json=make_intent(idempotency_key="one-time-1", approval_id=approval_id),
    )
    assert first.status_code == 200
    adapter = get_adapter(Market.A_SHARE)
    assert len(adapter.submitted) == 1

    second = client.post(
        "/v1/markets/A_SHARE/orders",
        json=make_intent(idempotency_key="one-time-2", approval_id=approval_id),
    )
    assert second.status_code == 409
    assert error_body(second)["error_code"] == "APPROVAL_ALREADY_CONSUMED"
    assert len(adapter.submitted) == 1

    stored = approval_store.get_approval(approval_id)
    assert stored is not None
    assert stored.status == ApprovalStatus.CONSUMED
    assert stored.consumed_order_id == first.json()["order_id"]


def test_retry_same_body_returns_original_no_second_order(client, risk_pass):
    """幂等重试：同键同体重放返回原订单，审批保持单次消费、venue 只收一笔。"""
    from quant_gateway.adapters import get_adapter

    register_risk_snapshot(make_snapshot())
    body = make_intent(idempotency_key="retry-same", approval_id=approved_approval())
    first = client.post("/v1/markets/A_SHARE/orders", json=body)
    assert first.status_code == 200
    original_id = first.json()["order_id"]

    retry = client.post("/v1/markets/A_SHARE/orders", json=body)
    assert retry.status_code == 409
    assert error_body(retry)["error_code"] == "DUPLICATE_ORDER"
    assert original_id in error_body(retry)["message"]
    assert len(get_adapter(Market.A_SHARE).submitted) == 1


def test_unbound_approval_cannot_authorize(client, risk_pass):
    """无绑定的订单审批即使已 APPROVED 也不能放行（失败关闭）。"""
    from quant_gateway.adapters import get_adapter

    register_risk_snapshot(make_snapshot())
    unbound = approval_store.create_approval(
        market=Market.A_SHARE,
        requested_by_bot="a-stock-bot",
        subject_type="order",
        subject_id="sub-unbound",
    ).approval_id
    unbound = approval_store.decide_approval(
        unbound, ApprovalStatus.APPROVED, "human"
    ).approval_id
    resp = client.post(
        "/v1/markets/A_SHARE/orders",
        json=make_intent(idempotency_key="unbound-1", approval_id=unbound),
    )
    assert resp.status_code == 409
    assert error_body(resp)["error_code"] == "APPROVAL_UNBOUNDED"
    assert get_adapter(Market.A_SHARE).submitted == []


def test_approval_consume_release_state_machine(client, risk_pass):
    """原子消费 + 失败释放 + 完成落账的状态机：
    APPROVED -> CONSUMING -> CONSUMED；FAILED 时回到 APPROVED 可重试。"""
    from quant_gateway import storage as gw_storage
    from quant_gateway.approval_store import (
        ClaimStatus,
        claim_order_reservation,
    )

    register_risk_snapshot(make_snapshot())
    approval_id = approved_approval()
    intent = OrderIntent.model_validate(make_intent(approval_id=approval_id))
    digest = orders_router.digest_from_intent(intent)

    status, _ = claim_order_reservation(approval_id, digest, "sm-key-1", "hash-a")
    assert status == ClaimStatus.OK
    stored = approval_store.get_approval(approval_id)
    assert stored is not None and stored.status == ApprovalStatus.CONSUMING
    assert stored.consumed_key == "sm-key-1"
    assert stored.consumed_request_hash == "hash-a"

    status2, _ = claim_order_reservation(approval_id, digest, "sm-key-1", "hash-a")
    assert status2 in (
        ClaimStatus.IDEMPOTENCY_IN_FLIGHT,
        ClaimStatus.APPROVAL_NOT_APPROVED,
    )
    stored = approval_store.get_approval(approval_id)
    assert stored is not None and stored.status == ApprovalStatus.CONSUMING

    gw_storage.mark_idempotency_failed("sm-key-1")
    stored = approval_store.get_approval(approval_id)
    assert stored is not None and stored.status == ApprovalStatus.APPROVED
    assert stored.consumed_key is None

    status3, _ = claim_order_reservation(approval_id, digest, "sm-key-2", "hash-b")
    assert status3 == ClaimStatus.OK
    gw_storage.finalize_idempotency_key("sm-key-2", "ord-sm")
    stored = approval_store.get_approval(approval_id)
    assert stored is not None and stored.status == ApprovalStatus.CONSUMED
    assert stored.consumed_order_id == "ord-sm"


# ---- P1: 拒绝路径审计 / venue 拒单释放 / STRONG 恢复 ----

def _audit_rows(action: str) -> list:
    from quant_gateway import audit as audit_mod
    from quant_gateway import storage as gw_storage
    with gw_storage.locked_conn() as conn:
        rows = conn.execute(
            "SELECT action, market, subject_id, outcome, detail FROM audit_log "
            "WHERE action = ? ORDER BY occurred_at", (action,)
        ).fetchall()
    keys = ("action", "market", "subject_id", "outcome", "detail")
    return [dict(zip(keys, r)) for r in rows]


def _patch_venue_reject(monkeypatch):
    """让当前 A_SHARE 适配器拒单(明确 ValueError,非未知状态)。"""
    from quant_gateway.adapters import get_adapter
    adapter = get_adapter(Market.A_SHARE)

    def reject(intent):
        raise ValueError("venue rejected: insufficient balance")

    monkeypatch.setattr(adapter, "request_order", reject)


def test_rejection_paths_are_audited(client, risk_pass):
    """风控拦截必须落审计:网关是审计权威,拒绝不可只活在响应里。"""
    resp = client.post("/v1/markets/A_SHARE/orders", json=make_intent())
    assert resp.status_code == 422
    rows = _audit_rows("order.rejected")
    assert rows, "RISK_SNAPSHOT_MISSING rejection must be audited"
    assert "error_code=RISK_SNAPSHOT_MISSING" in rows[-1]["detail"]
    assert rows[-1]["market"] == "A_SHARE"
    assert rows[-1]["subject_id"] == "key-1"


def test_data_unavailable_rejection_audited(client, monkeypatch):
    """equity 不可用拒单也要落审计(带 DATA_UNAVAILABLE)。"""
    from quant_gateway.adapters import get_adapter
    adapter = get_adapter(Market.A_SHARE)
    monkeypatch.setattr(adapter, "get_account_summary", lambda: [])
    register_risk_snapshot(make_snapshot())
    resp = client.post("/v1/markets/A_SHARE/orders", json=make_intent())
    assert resp.status_code == 422
    assert "equity unavailable" in resp.json()["detail"]["message"]
    rows = _audit_rows("order.rejected")
    assert any("error_code=DATA_UNAVAILABLE" in r["detail"] for r in rows)


def test_venue_reject_releases_key_and_approval(client, risk_pass, monkeypatch):
    """venue 明确拒单:幂等键 FAILED、审批释放回 APPROVED,同意图可重试。"""
    register_risk_snapshot(make_snapshot())
    approval_id = approved_approval()
    body = make_intent(idempotency_key="venue-rej-1", approval_id=approval_id)
    _patch_venue_reject(monkeypatch)

    resp = client.post("/v1/markets/A_SHARE/orders", json=body)
    assert resp.status_code == 422
    assert resp.json()["detail"]["error_code"] == "VENUE_REJECTED"

    from quant_gateway import storage as gw_storage
    # 审批已释放回 APPROVED(可按同一意图重试),消费痕迹清除
    approval = approval_store.get_approval(approval_id)
    assert approval.status == ApprovalStatus.APPROVED
    assert approval.consumed_key is None
    # 幂等键标 FAILED:同键同请求体可重走完整门禁
    record = gw_storage.get_idempotency_record("venue-rej-1")
    assert record["status"] == "FAILED"
    # 拒单事实落审计
    assert any(
        "error_code=VENUE_REJECTED" in r["detail"]
        for r in _audit_rows("order.rejected")
    )


def test_venue_reject_then_same_intent_can_retry(client, risk_pass, monkeypatch):
    """释放后:同一意图+新审批可重试成功。"""
    from quant_gateway import storage as gw_storage
    register_risk_snapshot(make_snapshot())
    first_approval = approved_approval()
    body = make_intent(idempotency_key="retry-1", approval_id=first_approval)
    from quant_gateway.adapters import get_adapter
    adapter = get_adapter(Market.A_SHARE)
    orig_submit = adapter.request_order
    adapter.request_order = lambda intent: (_ for _ in ()).throw(
        ValueError("venue rejected: limit move"))
    assert client.post("/v1/markets/A_SHARE/orders", json=body).status_code == 422
    adapter.request_order = orig_submit

    # 审批已释放回 APPROVED:同一审批 + 同一意图(同请求体)可重试。
    # 请求体哈希包含 approval_id——换审批会 409 IDEMPOTENCY_CONFLICT,
    # 这是刻意的严格语义:同键必须同请求体。
    resp = client.post(
        "/v1/markets/A_SHARE/orders",
        json=make_intent(idempotency_key="retry-1", approval_id=first_approval),
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "SUBMITTED"


def test_paper_adapter_declares_strong_consistency():
    from quant_gateway.adapters.paper import PaperAdapter
    assert PaperAdapter.order_lookup_consistency == "STRONG"
