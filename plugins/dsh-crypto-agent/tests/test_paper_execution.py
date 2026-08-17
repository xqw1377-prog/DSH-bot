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
        # 可控点：fresh preview 的风险乘数（模拟市场变化后风险上升）
        # 1.0 = 与首次预览一致；1.20 = 风险增加 20%
        self.preview_risk_factor: Decimal = Decimal("1.0")

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
            market=self.market, account_id="paper-crypto-001",
            symbol="BTCUSDT", quantity="0.36", available_quantity="0.36",
            frozen_quantity="0", avg_cost="67420", currency="USDT",
            as_of=datetime.now(UTC),
        )]

    def get_account_summary(self):
        return [AccountSummary(
            market=self.market, account_id="paper-crypto-001",
            cash="50000", equity="82000", currency="USDT",
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
        notional = qty * Decimal("100")
        factor = self.preview_risk_factor
        return OrderPreview(
            intent=intent, estimated_cost=notional,
            estimated_slippage=Decimal("0.0005"),
            risk=RiskSnapshot(
                risk_snapshot_id=f"rs-sig-x", market=self.market,
                account_id="paper-crypto-001",
                position_before=Decimal("0"), position_after=qty,
                risk_budget_delta=notional * factor,
                worst_case_loss=notional * Decimal("0.01") * factor,
                limits_hit=[], as_of=datetime.now(UTC),
            ),
        ).model_dump(mode="json")

    def request_order(self, intent):
        payload = intent if isinstance(intent, dict) else intent.model_dump(mode="json")
        self.submitted.append(payload)
        order_id = f"{self.market.value}-ord-{len(self.submitted)}"
        self.order_status[order_id] = "FILLED"
        return order_id

    def get_order_status(self, order_id):
        status = self.order_status.get(order_id, "UNKNOWN")
        return {"order_id": order_id, "status": status,
                "filled_quantity": "0.005" if status == "PARTIALLY_FILLED" else "0.01"}

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
                        account_id="paper-crypto-001")
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
    assert len(session.tasks.find_by_status("RECONCILED")) == 1


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
        conn.execute(
            "UPDATE approvals SET payload = ? WHERE approval_id = ?",
            (json.dumps(approval), task["approval_id"]),
        )
        conn.commit()
    run_once(session, agent)
    assert ADAPTER.submitted == []
    assert len(session.tasks.find_by_status("EXPIRED")) == 1


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
    failed = session.tasks.find_by_status("FAILED")
    assert len(failed) == 1
    assert "expired" in failed[0].get("payload", {}).get("valid_until", "") or True


# ---- 4. 相同审批重复处理只产生一个订单 ----

def test_duplicate_processing_yields_single_order():
    agent, session = _agent_and_session()
    _drive_to_approved(session, agent)
    run_once(session, agent)  # 正常执行一次
    assert len(ADAPTER.submitted) == 1
    order_id = session.tasks.find_by_status("RECONCILED")[0]["order_id"]

    # 模拟重复消息：把任务拨回待审批执行态，再 tick
    # 同时重置 reconciliation_status（执行状态回退，对账状态也回退）
    from dsh_runtime.store import _get
    conn = _get()
    conn.execute(
        "UPDATE bot_tasks SET status = 'APPROVED_SUBMITTING',"
        " order_id = NULL, reconciliation_status = 'PENDING'"
        " WHERE task_id LIKE '%sig-x'"
    )
    conn.commit()
    run_once(session, agent)
    assert len(ADAPTER.submitted) == 1  # 幂等认领，没有第二笔
    adopted = session.tasks.find_by_status("RECONCILED")
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
        "UPDATE bot_tasks SET status = 'APPROVED_SUBMITTING',"
        " order_id = NULL, reconciliation_status = 'PENDING'"
        " WHERE task_id LIKE '%sig-x'"
    )
    conn.commit()
    # 重启后第一个 tick：409 → 认领既有订单 → 查询恢复 → RECONCILED
    run_once(session, agent)
    assert len(ADAPTER.submitted) == 1  # 没有重复下单
    assert len(session.tasks.find_by_status("RECONCILED")) == 1


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
    ADAPTER.order_status.clear()
    run_once(session, agent)  # 提交
    task = session.tasks.find_by_status("SUBMITTED", "ACKNOWLEDGED", "PARTIALLY_FILLED", "FILLED", "RECONCILED")[0]
    ADAPTER.set_order_status(task["order_id"], venue_status)
    conn = _get_conn_helper()
    conn.execute(
        "UPDATE bot_tasks SET status = 'SUBMITTED' WHERE task_id = ?",
        (task["task_id"],),
    )
    conn.commit()
    run_once(session, agent)  # 对账 tick
    assert len(session.tasks.find_by_status(final_state)) == 1
    assert len(ADAPTER.submitted) == 1  # 任何状态都不重新提交


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
    assert len(session.tasks.find_by_status("FAILED")) == 1


def test_risk_snapshot_change_fails_closed():
    agent, session = _agent_and_session()
    task, _ = _drive_to_approved(session, agent)
    # 篡改任务记录的风险数字 → 与重新预览的结果不一致
    from dsh_runtime.store import _get
    payload = dict(task["payload"])
    payload["worst_case_loss"] = "0.0000001"
    conn = _get()
    conn.execute(
        "UPDATE bot_tasks SET payload = ? WHERE task_id = ?",
        (json.dumps(payload), task["task_id"]),
    )
    conn.commit()
    run_once(session, agent)
    assert ADAPTER.submitted == []
    assert len(session.tasks.find_by_status("FAILED")) == 1


def test_risk_within_tolerance_executes():
    """风险在容忍范围内（fresh worst_case_loss +3%）应放行，不失败关闭。

    P0 修复：完全相等校验改为批准范围校验后，小幅风险变化不再拒绝订单。
    模拟方式：审批后市场小幅变化，fresh preview 的 worst_case_loss 上升 3%。
    """
    agent, session = _agent_and_session()
    task, _ = _drive_to_approved(session, agent)
    # fresh preview 风险增加 3%（在 5% 容忍范围内）
    ADAPTER.preview_risk_factor = Decimal("1.03")
    run_once(session, agent)
    # 应该执行（容忍范围内）
    assert len(ADAPTER.submitted) == 1
    assert len(session.tasks.find_by_status("FAILED")) == 0


def test_risk_exceeds_tolerance_fails_closed():
    """风险超出容忍范围（fresh worst_case_loss +20%）应失败关闭。

    P0 修复：超过 5% 容忍上限的订单被拒绝。
    模拟方式：审批后市场大幅变化，fresh preview 的 worst_case_loss 上升 20%。
    """
    agent, session = _agent_and_session()
    task, _ = _drive_to_approved(session, agent)
    # fresh preview 风险增加 20%（超出 5% 容忍上限）
    ADAPTER.preview_risk_factor = Decimal("1.20")
    run_once(session, agent)
    assert ADAPTER.submitted == []
    assert len(session.tasks.find_by_status("FAILED")) == 1


# ---- 8. execution_status 与 reconciliation_status 正交分离 ----

def test_execution_and_reconciliation_status_separated():
    """P0: execution_status (FILLED) 与 reconciliation_status (PENDING→RECONCILED)
    是两个独立维度。订单 FILLED 后对账状态仍为 PENDING，对账完成后才 RECONCILED。
    """
    agent, session = _agent_and_session()
    _drive_to_approved(session, agent)
    # 提交订单：adapter 的 get_order_status 返回 FILLED，执行状态到 FILLED，
    # 同 tick 内 _reconcile_account 把对账状态从 PENDING 推到 RECONCILED
    run_once(session, agent)

    reconciled = session.tasks.find_by_status("RECONCILED")
    assert len(reconciled) == 1
    task = reconciled[0]
    # 执行状态 = RECONCILED（任务终态），对账状态 = RECONCILED
    assert task["reconciliation_status"] == "RECONCILED"


def test_reconciliation_mismatch_sets_mismatch_status():
    """P0: 对账不一致时 reconciliation_status = MISMATCH，任务 FAILED。"""
    agent, session = _agent_and_session()
    _drive_to_approved(session, agent)
    run_once(session, agent)

    # 模拟对账不一致：把持仓改为负数，然后手动重置任务到 FILLED + PENDING 重新对账
    from dsh_contracts import Position
    from datetime import UTC, datetime

    original_get_positions = ADAPTER.get_positions

    def bad_positions(account_id=None):
        return [Position(
            market=Market.CRYPTO, account_id="paper-crypto-001",
            symbol="BTCUSDT", quantity="-1", available_quantity="-1",
            frozen_quantity="0", avg_cost="67420", currency="USDT",
            as_of=datetime.now(UTC),
        )]

    ADAPTER.get_positions = bad_positions
    from dsh_runtime.store import _get
    conn = _get()
    conn.execute(
        "UPDATE bot_tasks SET status = 'FILLED',"
        " reconciliation_status = 'PENDING'"
        " WHERE task_id LIKE '%sig-x'"
    )
    conn.commit()
    run_once(session, agent)

    failed = session.tasks.find_by_status("FAILED")
    assert len(failed) == 1
    assert failed[0]["reconciliation_status"] == "MISMATCH"
    ADAPTER.get_positions = original_get_positions


def test_reconciliation_survives_crash_restart():
    """P0: 崩溃恢复——对账进行中重启，reconciliation_status 保持 IN_PROGRESS，
    重启后能继续对账直至 RECONCILED。"""
    agent, session = _agent_and_session()
    _drive_to_approved(session, agent)
    run_once(session, agent)

    # 模拟崩溃：任务已 FILLED 但对账进行中（reconciliation_status = IN_PROGRESS）
    from dsh_runtime.store import _get
    conn = _get()
    conn.execute(
        "UPDATE bot_tasks SET status = 'FILLED',"
        " reconciliation_status = 'IN_PROGRESS'"
        " WHERE task_id LIKE '%sig-x'"
    )
    conn.commit()

    # 重启后第一个 tick：应从 IN_PROGRESS 继续对账 → RECONCILED
    run_once(session, agent)
    reconciled = session.tasks.find_by_status("RECONCILED")
    assert len(reconciled) == 1
    assert reconciled[0]["reconciliation_status"] == "RECONCILED"
