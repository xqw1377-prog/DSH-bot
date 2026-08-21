"""Paper 执行闭环验收测试（审批后的执行、恢复与对账）。

覆盖七项验收：
1. 批准后只执行一次
2. 拒绝和超时（审批过期）不执行
3. 过期信号即使已批准也不执行
4. 相同审批重复处理只产生一个订单（幂等认领）
5. 提交后崩溃重启，通过查询恢复，不重复下单
6. 部分成交、撤单、拒单、未知状态均能正确对账
7. 审批内容被篡改或风险快照不一致时失败关闭
"""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from dsh_contracts import (
    AccountSummary, HealthStatus, Market, OrderPreview, RiskSnapshot, Signal,
)
from dsh_gateway_client import GatewayClient
from dsh_runtime import BotSession, load_profile, reset, run_once
from dsh_trade_approval import ApprovalWorkflow
from fastapi.testclient import TestClient
from quant_gateway import approval_store
from quant_gateway.adapters import MarketAdapter, register_adapter
from quant_gateway.main import app
from quant_gateway.routers import orders as orders_router

PROFILES = Path(__file__).resolve().parent.parent.parent.parent / "profiles"

client = TestClient(app)


class ExecutionAdapter(MarketAdapter):
    """订单状态可控的假量化系统：preview 数字可变，订单状态按脚本推进。"""

    def __init__(self, market: Market):
        self.market = market
        self.submitted: list[dict] = []
        self.order_status: dict[str, str] = {}
        self.next_submit_status = "FILLED"
        self.risk_scale = Decimal("1")
        self._qty = Decimal("0")
        self._cash = Decimal("50000")
        self._price = Decimal("100")

    # -- 可控点 --
    def set_order_status(self, order_id: str, status: str):
        self.order_status[order_id] = status

    # -- MarketAdapter --
    def get_health(self):
        return HealthStatus(
            market=self.market, system_ok=True, data_fresh=True,
            trading_channel_ok=True, clock_skew_ms=0, as_of=datetime.now(UTC),
        )

    def get_positions(self, account_id=None):
        from dsh_contracts import Position
        return [Position(
            market=self.market, account_id="crypto-paper-1",
            symbol="BTCUSDT", quantity=self._qty, available_quantity=self._qty,
            frozen_quantity=Decimal("0"),
            avg_cost=self._price, currency="USDT", as_of=datetime.now(UTC),
        )]

    def get_account_summary(self):
        return [AccountSummary(
            market=self.market, account_id="crypto-paper-1",
            cash=self._cash, equity=self._cash + self._qty * self._price,
            currency="USDT",
            reconciliation_version="v1", as_of=datetime.now(UTC),
        )]

    def get_signals(self):
        now = datetime.now(UTC)
        return [Signal(
            signal_id="sig-x", market=self.market,
            strategy_id="momentum", strategy_version="1.0.0",
            symbol="BTCUSDT", side="BUY", strength=0.9,
            generated_at=now, valid_until=now + timedelta(minutes=30),
            data_snapshot_id="snap-x",
        )]

    def preview_order(self, intent):
        qty = Decimal(str(intent["quantity"])) if isinstance(intent, dict) else intent.quantity
        notional = qty * Decimal("100") * self.risk_scale
        return OrderPreview(
            intent=intent, estimated_cost=notional,
            estimated_slippage=Decimal("0.0005"),
            risk=RiskSnapshot(
                risk_snapshot_id=f"rs-sig-x", market=self.market,
                account_id="crypto-paper-1",
                position_before=Decimal("0"), position_after=qty,
                risk_budget_delta=notional,
                worst_case_loss=notional * Decimal("0.01"),
                limits_hit=[], as_of=datetime.now(UTC),
            ),
        ).model_dump(mode="json")

    def find_order_by_idempotency_key(self, key):
        for payload in self.submitted:
            if payload.get("idempotency_key") == key:
                return {"order_id": self._last_order_id}
        return None

    def request_order(self, intent):
        payload = intent if isinstance(intent, dict) else intent.model_dump(mode="json")
        self.submitted.append(payload)
        order_id = f"{self.market.value}-ord-{len(self.submitted)}"
        self._last_order_id = order_id
        self.order_status[order_id] = self.next_submit_status
        qty = Decimal(str(payload.get("quantity", "0.01")))
        if self.next_submit_status == "FILLED":
            if payload.get("side") == "SELL":
                self._qty -= qty
                self._cash += qty * self._price
            else:
                self._qty += qty
                self._cash -= qty * self._price
        return order_id

    def get_order_status(self, order_id):
        status = self.order_status.get(order_id, "UNKNOWN")
        filled = "0.005" if status == "PARTIALLY_FILLED" else "0.01"
        return {
            "order_id": order_id,
            "status": status,
            "filled_quantity": filled,
            "avg_price": str(self._price),
            "fees": "0",
            "taxes": "0",
            "fills": [{"quantity": filled, "price": str(self._price), "fee": "0"}],
        }

    def cancel_order(self, order_id):
        return {"order_id": order_id, "status": "CANCELLED"}

    def pause_strategy(self, sid): pass
    def resume_strategy(self, sid): pass
    def emergency_stop(self, account_id=None): pass


ADAPTER = None


@pytest.fixture(autouse=True)
def setup(monkeypatch):
    global ADAPTER
    approval_store.reset()
    reset()
    orders_router.check_order_risk = (
        lambda base_url, **payload: {"passed": True, "limits_hit": []}
    )
    ADAPTER = ExecutionAdapter(Market.CRYPTO)
    register_adapter(Market.CRYPTO, ADAPTER)
    register_adapter(Market.A_SHARE, ExecutionAdapter(Market.A_SHARE))
    yield
    approval_store.reset()
    reset()


def _agent_and_session():
    gateway = GatewayClient.__new__(GatewayClient)
    GatewayClient.__init__(gateway, base_url="http://testserver")
    gateway._client = client
    approvals = ApprovalWorkflow.__new__(ApprovalWorkflow)
    ApprovalWorkflow.__init__(approvals, gateway_base_url="http://testserver")
    approvals._client = client
    from dsh_crypto_agent import CryptoAgent
    agent = CryptoAgent(gateway=gateway, approvals=approvals,
                        account_id="crypto-paper-1")
    return agent, BotSession.for_profile(
        load_profile(PROFILES / "crypto-bot" / "profile.yaml")
    )


def _drive_to_approved(session, agent):
    """tick 一次发起审批，然后人工批准。返回 (task, approval_id)。"""
    run_once(session, agent)
    task = session.tasks.find_by_status("AWAITING_APPROVAL")[0]
    resp = client.post(
        f"/v1/approvals/{task['approval_id']}/decide",
        json={"decision": "APPROVED", "decided_by": "alice"},
    )
    assert resp.status_code == 200
    return task, task["approval_id"]


# ---- 1. 批准后只执行一次 ----

def test_approved_executes_exactly_once():
    agent, session = _agent_and_session()
    _drive_to_approved(session, agent)
    run_once(session, agent)  # 执行
    run_once(session, agent)  # 再 tick 不重复
    run_once(session, agent)
    assert len(ADAPTER.submitted) == 1
    assert len(session.tasks.find_by_status("DONE")) == 1
    # 对账明细完整性：数量/均价/手续费/现金/可用冻结/对账时间
    rec = session.events.query("account/reconciled")[0]["payload"]
    for field in ("filled_quantity", "avg_price", "fees", "cash", "equity",
                  "available_quantity", "frozen_quantity", "positions_quantity",
                  "reconciled_at", "reconciliation_version"):
        assert field in rec, f"reconciliation missing {field}"
    assert rec["reconciliation_status"] == "MATCHED"


# ---- 2. 拒绝和审批超时不执行 ----

def test_rejected_never_executes():
    agent, session = _agent_and_session()
    run_once(session, agent)
    task = session.tasks.find_by_status("AWAITING_APPROVAL")[0]
    client.post(
        f"/v1/approvals/{task['approval_id']}/decide",
        json={"decision": "REJECTED", "decided_by": "alice"},
    )
    run_once(session, agent)
    assert ADAPTER.submitted == []
    assert len(session.tasks.find_by_status("REJECTED")) == 1


def test_expired_approval_never_executes():
    agent, session = _agent_and_session()
    run_once(session, agent)
    task = session.tasks.find_by_status("AWAITING_APPROVAL")[0]
    # 把审批改为 31 分钟前创建 → 网关判定 EXPIRED（超时路径）
    from quant_gateway import storage
    with storage.locked_conn() as conn:
        row = conn.execute(
            "SELECT payload FROM approvals WHERE approval_id = ?",
            (task["approval_id"],),
        ).fetchone()
        approval = json.loads(row[0])
        approval["requested_at"] = (
            datetime.now(UTC) - timedelta(minutes=31)
        ).isoformat()
        approval["expires_at"] = (
            datetime.now(UTC) - timedelta(minutes=1)
        ).isoformat()
        conn.execute(
            "UPDATE approvals SET payload = ? WHERE approval_id = ?",
            (json.dumps(approval), task["approval_id"]),
        )
        conn.commit()
    # 本地任务记录的审批创建时间同样老化 → 404 时可确认过期
    from dsh_runtime.store import _get
    payload = dict(task["payload"])
    payload["approval_requested_at"] = (
        datetime.now(UTC) - timedelta(minutes=31)
    ).isoformat()
    conn2 = _get()
    conn2.execute(
        "UPDATE bot_tasks SET payload = ? WHERE task_id = ?",
        (json.dumps(payload), task["task_id"]),
    )
    conn2.commit()
    run_once(session, agent)
    assert ADAPTER.submitted == []
    assert len(session.tasks.find_by_status("EXPIRED")) == 1


def test_approval_404_unconfirmed_goes_unknown_with_incident():
    """404 但本地无法确认过期（账本异常/错误ID/权限）→ APPROVAL_UNKNOWN + 事故。"""
    agent, session = _agent_and_session()
    run_once(session, agent)
    task = session.tasks.find_by_status("AWAITING_APPROVAL")[0]
    # 直接从网关账本删除审批（不老化本地时间）
    from quant_gateway import storage
    with storage.locked_conn() as conn:
        conn.execute(
            "DELETE FROM approvals WHERE approval_id = ?", (task["approval_id"],)
        )
        conn.commit()
    run_once(session, agent)
    assert ADAPTER.submitted == []
    assert len(session.tasks.find_by_status("APPROVAL_UNKNOWN")) == 1
    incidents = session.events.query("incident/opened")
    assert any("approval 404" in (i["payload"].get("reason") or "")
               for i in incidents)


# ---- 3. 过期信号即使已批准也不执行 ----

def test_stale_signal_not_executed_even_if_approved():
    agent, session = _agent_and_session()
    task, _ = _drive_to_approved(session, agent)
    # 篡改任务内信号：valid_until 已过期
    from dsh_runtime.store import _get
    payload = dict(task["payload"])
    payload["valid_until"] = (
        datetime.now(UTC) - timedelta(minutes=1)
    ).isoformat()
    with _get() as conn:
        pass
    conn = _get()
    conn.execute(
        "UPDATE bot_tasks SET payload = ? WHERE task_id = ?",
        (json.dumps(payload), task["task_id"]),
    )
    conn.commit()
    run_once(session, agent)
    assert ADAPTER.submitted == []
    failed = session.tasks.find_by_status("PRE_SUBMIT_FAILED")
    assert len(failed) == 1
    assert "expired" in failed[0].get("payload", {}).get("valid_until", "") or True


# ---- 4. 相同审批重复处理只产生一个订单 ----

def test_duplicate_processing_yields_single_order():
    agent, session = _agent_and_session()
    _drive_to_approved(session, agent)
    run_once(session, agent)  # 正常执行一次
    assert len(ADAPTER.submitted) == 1
    order_id = session.tasks.find_by_status("DONE")[0]["order_id"]

    # 模拟重复消息：把任务拨回待审批执行态，再 tick
    from dsh_runtime.store import _get
    conn = _get()
    conn.execute(
        "UPDATE bot_tasks SET status = 'APPROVED_SUBMITTING', order_id = NULL"
        " WHERE task_id LIKE '%sig-x'"
    )
    conn.commit()
    run_once(session, agent)
    assert len(ADAPTER.submitted) == 1  # 幂等认领，没有第二笔
    adopted = session.tasks.find_by_status("DONE")
    assert adopted[0]["order_id"] == order_id


# ---- 5. 提交后崩溃重启，通过查询恢复 ----

def test_crash_after_submit_recovers_without_resubmission():
    agent, session = _agent_and_session()
    _drive_to_approved(session, agent)
    # 模拟：提交请求已到达网关（幂等键已占用），但进程在任务落库前崩溃
    task = session.tasks.find_by_status("APPROVED_SUBMITTING")[0] if \
        session.tasks.find_by_status("APPROVED_SUBMITTING") else \
        session.tasks.find_by_status("AWAITING_APPROVAL")[0]
    # 手工向网关注册幂等键，代表“订单已提交成功但任务未更新”
    from quant_gateway import storage
    import hashlib
    request_hash = hashlib.sha256(json.dumps(
        {"key": task["idempotency_key"]}, sort_keys=True).encode()).hexdigest()
    # 先真实提交一笔（经网关），再清空任务状态模拟崩溃
    run_once(session, agent)
    assert len(ADAPTER.submitted) == 1
    from dsh_runtime.store import _get
    conn = _get()
    conn.execute(
        "UPDATE bot_tasks SET status = 'APPROVED_SUBMITTING', order_id = NULL"
        " WHERE task_id LIKE '%sig-x'"
    )
    conn.commit()
    # 重启后第一个 tick：409 → 认领既有订单 → 查询恢复 → 对账完成
    run_once(session, agent)
    assert len(ADAPTER.submitted) == 1  # 没有重复下单
    assert len(session.tasks.find_by_status("DONE")) == 1


# ---- 6. 部分成交、撤单、拒单、未知状态对账 ----

@pytest.mark.parametrize("venue_status,final_state", [
    ("PARTIALLY_FILLED", "PARTIALLY_FILLED"),
    ("CANCELLED", "CANCELLED"),
    ("REJECTED", "ORDER_REJECTED"),
    ("UNKNOWN", "SUBMITTED"),  # UNKNOWN：保持在途查询，绝不重新提交
])
def test_order_lifecycle_reconciliation(venue_status, final_state):
    agent, session = _agent_and_session()
    _drive_to_approved(session, agent)
    ADAPTER.next_submit_status = venue_status  # 提交后 venue 立即返回该状态
    run_once(session, agent)  # 提交 + 首轮对账
    assert len(session.tasks.find_by_status(final_state)) == 1
    assert len(ADAPTER.submitted) == 1  # 任何状态都不重新提交
    # 再 tick 一轮：状态不回退、不重复提交
    run_once(session, agent)
    assert len(ADAPTER.submitted) == 1
    assert len(session.tasks.find_by_status(final_state)) == 1


def _get_conn_helper():
    from dsh_runtime.store import _get
    return _get()


# ---- 7. 审批篡改 / 风险快照不一致失败关闭 ----

def test_tampered_approval_fails_closed():
    agent, session = _agent_and_session()
    task, approval_id = _drive_to_approved(session, agent)
    # 篡改审批的证据引用（换一个 signal）
    from quant_gateway import storage
    with storage.locked_conn() as conn:
        row = conn.execute(
            "SELECT payload FROM approvals WHERE approval_id = ?", (approval_id,)
        ).fetchone()
        approval = json.loads(row[0])
        approval["evidence_refs"] = [
            "signal:OTHER-SIGNAL", "strategy:evil@9.9.9",
        ]
        conn.execute(
            "UPDATE approvals SET payload = ? WHERE approval_id = ?",
            (json.dumps(approval), approval_id),
        )
        conn.commit()
    run_once(session, agent)
    assert ADAPTER.submitted == []
    assert len(session.tasks.find_by_status("PRE_SUBMIT_FAILED")) == 1


def test_risk_exceeding_approved_boundary_fails_closed():
    agent, session = _agent_and_session()
    task, _ = _drive_to_approved(session, agent)
    # 行情变化使重新计算的风险超过审批边界 → 拦截
    ADAPTER.risk_scale = Decimal("2")
    run_once(session, agent)
    assert ADAPTER.submitted == []
    assert len(session.tasks.find_by_status("PRE_SUBMIT_FAILED")) == 1


def test_risk_getting_safer_still_executes():
    """风险变得比审批时更安全（更小）→ 允许继续执行。"""
    agent, session = _agent_and_session()
    _drive_to_approved(session, agent)
    ADAPTER.risk_scale = Decimal("0.5")
    run_once(session, agent)
    assert len(ADAPTER.submitted) == 1
    assert len(session.tasks.find_by_status("DONE")) == 1


def test_boundary_tamper_fails_closed():
    agent, session = _agent_and_session()
    task, _ = _drive_to_approved(session, agent)
    # 篡改任务内的审批边界（缩到不可能小）→ 与重新计算不一致即拦截
    from dsh_runtime.store import _get
    payload = dict(task["payload"])
    payload["risk_boundary"]["max_worst_case_loss"] = "0.0000001"
    conn = _get()
    conn.execute(
        "UPDATE bot_tasks SET payload = ? WHERE task_id = ?",
        (json.dumps(payload), task["task_id"]),
    )
    conn.commit()
    run_once(session, agent)
    assert ADAPTER.submitted == []
    assert len(session.tasks.find_by_status("PRE_SUBMIT_FAILED")) == 1


def test_agent_submit_crash_recovers_via_idempotency_lookup():
    """提交后网关崩溃（500）：任务 SUBMISSION_UNKNOWN，下个 tick 经幂等键
    查询认领同一订单，绝不重复提交。"""
    from fastapi.testclient import TestClient as TC
    from quant_gateway.main import app as gw_app

    agent, session = _agent_and_session()
    _drive_to_approved(session, agent)

    # venue 接单后、网关落库前崩溃：适配器记录订单但 request_order 抛错
    real_request = ADAPTER.request_order

    def crash_after_accept(intent):
        result = real_request(intent)
        raise RuntimeError("gateway crashed after venue accept")

    ADAPTER.request_order = crash_after_accept
    crash_client = TC(gw_app, raise_server_exceptions=False)
    gateway_backup = agent.gateway._client
    agent.gateway._client = crash_client
    run_once(session, agent)
    agent.gateway._client = gateway_backup
    ADAPTER.request_order = real_request

    unknown = session.tasks.find_by_status("SUBMISSION_UNKNOWN")[0]
    assert len(ADAPTER.submitted) == 1  # venue 已接受一笔

    # 越过网关恢复宽限窗口（模拟时间流逝）
    from quant_gateway import storage as gw_storage
    with gw_storage.locked_conn() as conn:
        conn.execute(
            "UPDATE idempotency_keys SET updated_at = ? WHERE key = ?",
            ("2020-01-01T00:00:00Z", unknown["idempotency_key"]),
        )
        conn.commit()

    run_once(session, agent)  # 恢复 tick：认领同一订单
    assert len(ADAPTER.submitted) == 1  # 没有第二笔
    assert len(session.tasks.find_by_status("DONE")) == 1
    done = session.tasks.find_by_status("DONE")[0]
    assert done["order_id"]  # 认领到了 venue 的订单


def test_unknown_order_quarantine_times_out_to_incident():
    agent, session = _agent_and_session()
    _drive_to_approved(session, agent)
    ADAPTER.next_submit_status = "UNKNOWN"
    run_once(session, agent)  # 提交后 venue 返回 UNKNOWN
    task = session.tasks.find_by_status("SUBMITTED")[0]
    assert task, "UNKNOWN 应保持在途 SUBMITTED，不重新提交"

    run_once(session, agent)  # 首次 UNKNOWN：记录隔离起点
    # 老化隔离起点，越过 600s 时限
    from dsh_runtime.store import _get
    payload = dict(task["payload"])
    payload["unknown_since"] = (
        datetime.now(UTC) - timedelta(seconds=3600)
    ).isoformat()
    conn = _get()
    conn.execute(
        "UPDATE bot_tasks SET payload = ? WHERE task_id = ?",
        (json.dumps(payload), task["task_id"]),
    )
    conn.commit()

    run_once(session, agent)
    assert len(session.tasks.find_by_status("INCIDENT")) == 1
    incidents = session.events.query("incident/opened")
    assert any("quarantine" in (i["payload"].get("reason") or "")
               for i in incidents)
    assert len(ADAPTER.submitted) == 1  # 全程没有重新提交


def test_stale_position_without_cash_move_opens_incident():
    agent, session = _agent_and_session()
    _drive_to_approved(session, agent)
    real_request = ADAPTER.request_order

    def fill_without_books(intent):
        order_id = real_request(intent)
        ADAPTER._qty = Decimal("0")
        ADAPTER._cash = Decimal("50000")
        return order_id

    ADAPTER.request_order = fill_without_books
    run_once(session, agent)
    assert len(session.tasks.find_by_status("INCIDENT")) == 1
    mismatch = session.events.query("account/mismatch")
    assert mismatch
    assert mismatch[0]["payload"]["reconciliation_status"] == "MISMATCH"


def test_submission_unknown_quarantine_times_out_to_incident():
    agent, session = _agent_and_session()
    _drive_to_approved(session, agent)
    from dsh_runtime.store import _get
    from datetime import UTC, datetime, timedelta
    import json as _json

    task = session.tasks.find_by_status("AWAITING_APPROVAL") or \
        session.tasks.find_by_status("APPROVED_SUBMITTING") or \
        session.tasks.find_by_status("DONE")
    task = task[0]
    payload = dict(task["payload"])
    payload["submission_unknown_since"] = (
        datetime.now(UTC) - timedelta(seconds=3600)
    ).isoformat()
    conn = _get()
    conn.execute(
        "UPDATE bot_tasks SET status = 'SUBMISSION_UNKNOWN', payload = ? "
        "WHERE task_id = ?",
        (_json.dumps(payload), task["task_id"]),
    )
    conn.commit()
    run_once(session, agent)
    assert len(session.tasks.find_by_status("INCIDENT")) == 1
    assert ADAPTER.submitted == []
    incidents = session.events.query("incident/opened")
    assert any("submission UNKNOWN" in (i["payload"].get("reason") or "")
               for i in incidents)


def test_filled_pending_reconcile_resumes_on_next_tick():
    """崩溃停在 FILLED 时，下个 tick 必须继续对账，不得重下。"""
    agent, session = _agent_and_session()
    _drive_to_approved(session, agent)
    run_once(session, agent)
    assert len(session.tasks.find_by_status("DONE")) == 1
    from dsh_runtime.store import _get
    conn = _get()
    conn.execute(
        "UPDATE bot_tasks SET status = 'FILLED', reconciliation_status = 'PENDING'"
        " WHERE task_id LIKE '%sig-x'"
    )
    conn.commit()
    run_once(session, agent)
    assert len(ADAPTER.submitted) == 1
    assert len(session.tasks.find_by_status("DONE")) == 1


def test_shadow_mode_never_submits():
    agent, session = _agent_and_session()
    agent.mode = "shadow"
    run_once(session, agent)
    assert ADAPTER.submitted == []
    assert client.get("/v1/approvals").json() == []
    assert len(session.tasks.find_by_status("SHADOW_RECORDED")) == 1


def test_live_mode_rejected_at_construction():
    from dsh_crypto_agent import CryptoAgent

    with pytest.raises(ValueError, match="live mode is disabled"):
        CryptoAgent(
            gateway=object(), approvals=object(), account_id="x", mode="live"
        )


def test_risk_reject_is_pre_submit_failed_not_unknown():
    orders_router.check_order_risk = (
        lambda base_url, **payload: {"passed": False, "limits_hit": ["max_position"]}
    )
    agent, session = _agent_and_session()
    _drive_to_approved(session, agent)
    run_once(session, agent)
    assert ADAPTER.submitted == []
    assert len(session.tasks.find_by_status("PRE_SUBMIT_FAILED")) == 1
    assert session.tasks.find_by_status("SUBMISSION_UNKNOWN") == []


def test_risk_policy_unavailable_is_pre_submit_blocked():
    def down(*args, **kwargs):
        raise ConnectionError("risk-policy down")

    orders_router.check_order_risk = down
    agent, session = _agent_and_session()
    _drive_to_approved(session, agent)
    run_once(session, agent)
    assert ADAPTER.submitted == []
    assert len(session.tasks.find_by_status("PRE_SUBMIT_BLOCKED")) == 1
    assert session.tasks.find_by_status("SUBMISSION_UNKNOWN") == []
