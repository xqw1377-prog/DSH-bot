"""A 股执行闭环验收测试（审批后的执行、A 股特性校验、对账）。

覆盖七项验收：
1. 审批后执行：APPROVED → 提交 → ACK → FILLED → RECONCILED
2. 100 股整手校验：非 100 整数倍失败关闭
3. T+1 可卖数量：SELL 时 available_quantity 不足失败关闭
4. 交易时段校验：非交易时段拒绝提交
5. 涨跌停校验：触及涨停拒绝买入、跌停拒绝卖出
6. 部分成交、撤单、拒单的状态推进
7. 成交回写和资金持仓对账（reconciliation_status 分离）
"""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest
from dsh_contracts import (
    AccountSummary, HealthStatus, Market, OrderPreview, OrderSide,
    Position, RiskSnapshot, Signal,
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


class AStockExecAdapter(MarketAdapter):
    """订单状态可控的假 A 股量化系统。

    模拟真实 venue 行为：订单成交后自动更新持仓与资金，
    使对账能在成交后查到持仓变化。T+1：BUY 当日 available_quantity=0。
    """

    def __init__(self, market: Market, signals=None, positions=None,
                 limits_hit=None, initial_order_status="FILLED"):
        self.market = market
        self._signals = signals or []
        self._positions: list[Position] = list(positions or [])
        self._limits_hit = limits_hit or []
        self.submitted: list[dict] = []
        self.order_status: dict[str, str] = {}
        self._filled_qty: dict[str, Decimal] = {}
        self.initial_order_status = initial_order_status  # 提交后的初始 venue 状态

    def set_order_status(self, order_id: str, status: str):
        self.order_status[order_id] = status

    def get_health(self):
        return HealthStatus(
            market=self.market, system_ok=True, data_fresh=True,
            trading_channel_ok=True, clock_skew_ms=0, as_of=datetime.now(UTC),
        )

    def get_positions(self, account_id=None):
        return list(self._positions)

    def get_account_summary(self):
        return [AccountSummary(
            market=self.market, account_id="a-stock-paper-1",
            cash="100000", equity="100000", currency="CNY",
            reconciliation_version="v1", as_of=datetime.now(UTC),
        )]

    def get_signals(self):
        return list(self._signals)

    def preview_order(self, intent):
        qty = Decimal(str(intent["quantity"])) if isinstance(intent, dict) else intent.quantity
        notional = qty * Decimal("10")
        return OrderPreview(
            intent=intent, estimated_cost=notional,
            estimated_slippage=Decimal("0.1"),
            risk=RiskSnapshot(
                risk_snapshot_id=f"rs-{self.market.value}", market=self.market,
                account_id="a-stock-paper-1",
                position_before=Decimal("0"), position_after=qty,
                risk_budget_delta=notional,
                worst_case_loss=notional * Decimal("0.1"),
                limits_hit=list(self._limits_hit), as_of=datetime.now(UTC),
            ),
        ).model_dump(mode="json")

    def _apply_fill(self, payload: dict, order_id: str, qty: Decimal):
        """成交后更新持仓（模拟 venue 回写）。T+1：BUY 当日不可卖。"""
        symbol = payload.get("symbol", "600519.SH")
        side = payload.get("side", "BUY")
        idx = next((i for i, p in enumerate(self._positions)
                    if p.symbol == symbol), None)
        if side == "BUY":
            if idx is None:
                self._positions.append(Position(
                    market=self.market, account_id="a-stock-paper-1",
                    symbol=symbol, quantity=qty,
                    available_quantity=Decimal("0"),  # T+1：今日买入冻结
                    frozen_quantity=qty, avg_cost=Decimal("10"),
                    currency="CNY", as_of=datetime.now(UTC),
                ))
            else:
                old = self._positions[idx]
                self._positions[idx] = Position(
                    market=old.market, account_id=old.account_id,
                    symbol=old.symbol, quantity=old.quantity + qty,
                    available_quantity=old.available_quantity,  # T+1
                    frozen_quantity=old.frozen_quantity + qty,
                    avg_cost=old.avg_cost, currency=old.currency,
                    as_of=datetime.now(UTC),
                )
        elif side == "SELL":
            if idx is not None:
                old = self._positions[idx]
                new_qty = old.quantity - qty
                new_avail = max(Decimal("0"), old.available_quantity - qty)
                if new_qty <= 0:
                    self._positions.pop(idx)
                else:
                    self._positions[idx] = Position(
                        market=old.market, account_id=old.account_id,
                        symbol=old.symbol, quantity=new_qty,
                        available_quantity=new_avail,
                        frozen_quantity=old.frozen_quantity,
                        avg_cost=old.avg_cost, currency=old.currency,
                        as_of=datetime.now(UTC),
                    )

    def request_order(self, intent):
        payload = intent if isinstance(intent, dict) else intent.model_dump(mode="json")
        self.submitted.append(payload)
        order_id = f"{self.market.value}-ord-{len(self.submitted)}"
        self.order_status[order_id] = self.initial_order_status
        qty = Decimal(str(payload.get("quantity", "100")))
        self._filled_qty[order_id] = qty
        # 仅 FILLED 时才回写持仓（PARTIALLY_FILLED 由 get_order_status 阶段处理）
        if self.initial_order_status == "FILLED":
            self._apply_fill(payload, order_id, qty)
        return order_id  # MarketAdapter 契约：返回权威订单 ID 字符串

    def get_order_status(self, order_id):
        status = self.order_status.get(order_id, "FILLED")
        filled = self._filled_qty.get(order_id, Decimal("100"))
        if status == "PARTIALLY_FILLED":
            filled = filled // 2
        return {
            "order_id": order_id, "status": status, "symbol": "600519.SH",
            "filled_quantity": str(filled),
            "avg_price": "10", "fees": "0",
            "filled_at": datetime.now(UTC).isoformat(),
        }

    def cancel_order(self, order_id):
        return {"order_id": order_id, "status": "CANCELLED"}

    def pause_strategy(self, strategy_id):
        pass

    def resume_strategy(self, strategy_id):
        pass

    def emergency_stop(self, account_id=None):
        pass


ADAPTER: AStockExecAdapter = None  # type: ignore


@pytest.fixture(autouse=True)
def _setup_gateway(monkeypatch):
    global ADAPTER
    approval_store.reset()
    reset()
    # mock risk check
    orders_router.check_order_risk = (
        lambda base_url, **payload: {"passed": True, "limits_hit": []}
    )
    # mock trading hours: 默认返回 True（在交易时段内）
    monkeypatch.setattr(
        "dsh_a_stock_agent.agent._is_trading_hours", lambda dt: True
    )
    ADAPTER = AStockExecAdapter(Market.A_SHARE)
    register_adapter(Market.A_SHARE, ADAPTER)
    yield
    approval_store.reset()
    reset()


def _signal(side=OrderSide.BUY, strength=0.8, symbol="600519.SH",
            signal_id="sig-a-001", quantity="100"):
    now = datetime.now(UTC)
    return Signal(
        signal_id=signal_id, market=Market.A_SHARE,
        strategy_id="mean-reversion", strategy_version="1.0.0",
        symbol=symbol, side=side, strength=strength,
        generated_at=now, valid_until=now + timedelta(minutes=30),
        data_snapshot_id="snap-1",
    )


def _position(symbol="600519.SH", available="100"):
    return Position(
        market=Market.A_SHARE, account_id="a-stock-paper-1",
        symbol=symbol, quantity=Decimal("100"),
        available_quantity=Decimal(available),
        frozen_quantity=Decimal("0"), avg_cost=Decimal("10"),
        currency="CNY", as_of=datetime.now(UTC),
    )


def _agent_and_session(signals=None, positions=None, limits_hit=None,
                      initial_order_status="FILLED"):
    global ADAPTER
    ADAPTER = AStockExecAdapter(
        Market.A_SHARE, signals=signals, positions=positions,
        limits_hit=limits_hit, initial_order_status=initial_order_status,
    )
    register_adapter(Market.A_SHARE, ADAPTER)
    gateway = GatewayClient.__new__(GatewayClient)
    GatewayClient.__init__(gateway, base_url="http://testserver")
    gateway._client = client
    approvals = ApprovalWorkflow.__new__(ApprovalWorkflow)
    ApprovalWorkflow.__init__(approvals, gateway_base_url="http://testserver")
    approvals._client = client
    from dsh_a_stock_agent import AStockAgent
    agent = AStockAgent(gateway=gateway, approvals=approvals,
                        account_id="a-stock-paper-1")
    profile = load_profile(PROFILES / "a-stock-bot" / "profile.yaml")
    return agent, BotSession.for_profile(profile)


def _drive_to_approved(session, agent):
    """tick 一次发起审批，然后人工批准。返回 (task, approval_id)。"""
    run_once(session, agent)
    task = session.tasks.find_by_status("AWAITING_APPROVAL")[0]
    resp = client.post(
        f"/v1/approvals/{task['approval_id']}/decide",
        json={"decision": "APPROVED", "decided_by": "risk-officer"},
    )
    assert resp.status_code == 200
    return task, task["approval_id"]


# ---- 1. 审批后执行闭环 ----

def test_approved_executes_to_reconciled():
    """审批通过 → 提交 → ACK → FILLED → 对账 → RECONCILED。"""
    agent, session = _agent_and_session(signals=[_signal()])
    _drive_to_approved(session, agent)
    run_once(session, agent)  # 执行+对账
    run_once(session, agent)  # 确保推进到 RECONCILED

    assert len(ADAPTER.submitted) == 1
    reconciled = session.tasks.find_by_status("RECONCILED")
    assert len(reconciled) == 1
    assert reconciled[0]["reconciliation_status"] == "RECONCILED"


def test_rejected_approval_does_not_execute():
    """审批拒绝 → 不下单，任务 REJECTED。"""
    agent, session = _agent_and_session(signals=[_signal()])
    run_once(session, agent)
    task = session.tasks.find_by_status("AWAITING_APPROVAL")[0]
    client.post(
        f"/v1/approvals/{task['approval_id']}/decide",
        json={"decision": "REJECTED", "decided_by": "risk-officer"},
    )
    run_once(session, agent)
    assert ADAPTER.submitted == []
    assert len(session.tasks.find_by_status("REJECTED")) == 1


# ---- 2. 100 股整手校验 ----

def test_non_lot_size_fails_closed():
    """数量非 100 整数倍 → 执行前校验失败，任务 FAILED。"""
    # 信号带 quantity=150（非 100 整数倍）
    sig = _signal()
    sig = sig.model_copy(update={})  # Signal 是 frozen
    # 直接构造一个 quantity=150 的信号
    now = datetime.now(UTC)
    bad_signal = Signal(
        signal_id="sig-bad-lot", market=Market.A_SHARE,
        strategy_id="mean-reversion", strategy_version="1.0.0",
        symbol="600519.SH", side=OrderSide.BUY, strength=0.9,
        generated_at=now, valid_until=now + timedelta(minutes=30),
        data_snapshot_id="snap-bad",
    )
    agent, session = _agent_and_session(signals=[bad_signal])
    _drive_to_approved(session, agent)

    # 篡改 task payload 的 quantity 为 150（非整手）
    from dsh_runtime.store import _get
    task = session.tasks.find_by_status("AWAITING_APPROVAL")[0]
    payload = dict(task["payload"])
    payload["quantity"] = "150"
    conn = _get()
    conn.execute(
        "UPDATE bot_tasks SET payload = ? WHERE task_id = ?",
        (json.dumps(payload), task["task_id"]),
    )
    conn.commit()

    run_once(session, agent)
    assert ADAPTER.submitted == []
    assert len(session.tasks.find_by_status("FAILED")) == 1


# ---- 3. T+1 可卖数量校验 ----

def test_t_plus_1_sell_insufficient_available_fails():
    """SELL 时 available_quantity 不足 → 执行前校验失败（T+1 约束）。"""
    agent, session = _agent_and_session(
        signals=[_signal(side=OrderSide.SELL, signal_id="sig-sell-t1")],
        positions=[_position(available="50")],  # 只有 50 股可卖
    )
    _drive_to_approved(session, agent)
    run_once(session, agent)
    assert ADAPTER.submitted == []
    failed = session.tasks.find_by_status("FAILED")
    assert len(failed) == 1
    # 记忆中应有 T+1 相关信息
    mem = session.memory.recent(kind="error")
    assert any("T+1" in m["content"] or "可用持仓" in m["content"] for m in mem)


# ---- 4. 交易时段校验 ----

def test_non_trading_hours_fails_closed(monkeypatch):
    """非交易时段 → 执行前校验失败。"""
    monkeypatch.setattr(
        "dsh_a_stock_agent.agent._is_trading_hours", lambda dt: False
    )
    agent, session = _agent_and_session(signals=[_signal()])
    _drive_to_approved(session, agent)
    run_once(session, agent)
    assert ADAPTER.submitted == []
    failed = session.tasks.find_by_status("FAILED")
    assert len(failed) == 1
    assert any("trading hours" in m["content"] for m in session.memory.recent(kind="error"))


# ---- 5. 涨跌停校验 ----

def test_limit_up_rejects_buy():
    """触及涨停 → BUY 信号失败关闭（无法买入）。"""
    agent, session = _agent_and_session(
        signals=[_signal(signal_id="sig-limit-up")],
        limits_hit=["LIMIT_UP"],
    )
    _drive_to_approved(session, agent)
    run_once(session, agent)
    assert ADAPTER.submitted == []
    failed = session.tasks.find_by_status("FAILED")
    assert len(failed) == 1
    assert any("涨停" in m["content"] for m in session.memory.recent(kind="error"))


def test_limit_down_rejects_sell():
    """触及跌停 → SELL 信号失败关闭（无法卖出）。"""
    agent, session = _agent_and_session(
        signals=[_signal(side=OrderSide.SELL, signal_id="sig-limit-down")],
        positions=[_position(available="100")],
        limits_hit=["LIMIT_DOWN"],
    )
    _drive_to_approved(session, agent)
    run_once(session, agent)
    assert ADAPTER.submitted == []
    failed = session.tasks.find_by_status("FAILED")
    assert len(failed) == 1
    assert any("跌停" in m["content"] for m in session.memory.recent(kind="error"))


# ---- 6. 部分成交、撤单、拒单 ----

def test_partially_filled_then_filled():
    """部分成交 → 最终全部成交 → RECONCILED。"""
    # 使用 ACKNOWLEDGED 初始状态，让订单不立即成交，便于测试部分成交
    agent, session = _agent_and_session(signals=[_signal()],
                                         initial_order_status="ACKNOWLEDGED")
    _drive_to_approved(session, agent)
    run_once(session, agent)  # 提交 → ACKNOWLEDGED

    task = session.tasks.find_by_status("SUBMITTED", "ACKNOWLEDGED")
    assert task, f"任务未进入执行状态, tasks={session.tasks.find_by_status()}"
    actual_order_id = task[0].get("order_id")
    assert actual_order_id, "order_id 为空"

    # 推进到 ACKNOWLEDGED
    run_once(session, agent)
    # 设置部分成交
    ADAPTER.set_order_status(actual_order_id, "PARTIALLY_FILLED")
    run_once(session, agent)
    assert len(session.tasks.find_by_status("PARTIALLY_FILLED")) == 1

    # 最终全部成交
    ADAPTER.set_order_status(actual_order_id, "FILLED")
    # FILLED 时需要回写持仓
    payload = ADAPTER.submitted[0]
    ADAPTER._apply_fill(payload, actual_order_id,
                        Decimal(str(payload.get("quantity", "100"))))
    run_once(session, agent)  # FILLED → 对账
    run_once(session, agent)  # 确保 RECONCILED
    reconciled = session.tasks.find_by_status("RECONCILED")
    assert len(reconciled) == 1


def test_cancelled_order():
    """订单被撤单 → 任务 CANCELLED。"""
    agent, session = _agent_and_session(signals=[_signal()],
                                         initial_order_status="ACKNOWLEDGED")
    _drive_to_approved(session, agent)
    run_once(session, agent)  # 提交 → ACKNOWLEDGED
    task = session.tasks.find_by_status("SUBMITTED", "ACKNOWLEDGED")
    assert task, "任务未进入执行状态"
    actual_order_id = task[0].get("order_id")
    ADAPTER.set_order_status(actual_order_id, "CANCELLED")
    run_once(session, agent)
    cancelled = session.tasks.find_by_status("CANCELLED")
    assert len(cancelled) == 1


def test_rejected_order():
    """订单被拒单 → 任务 ORDER_REJECTED。"""
    agent, session = _agent_and_session(signals=[_signal()],
                                         initial_order_status="ACKNOWLEDGED")
    _drive_to_approved(session, agent)
    run_once(session, agent)  # 提交 → ACKNOWLEDGED
    task = session.tasks.find_by_status("SUBMITTED", "ACKNOWLEDGED")
    assert task, "任务未进入执行状态"
    actual_order_id = task[0].get("order_id")
    ADAPTER.set_order_status(actual_order_id, "REJECTED")
    run_once(session, agent)
    rejected = session.tasks.find_by_status("ORDER_REJECTED")
    assert len(rejected) == 1


# ---- 7. 成交回写和资金持仓对账（reconciliation_status 分离）----

def test_reconciliation_status_separated():
    """execution_status (FILLED) 与 reconciliation_status (RECONCILED) 分离。"""
    agent, session = _agent_and_session(signals=[_signal()])
    _drive_to_approved(session, agent)
    run_once(session, agent)
    run_once(session, agent)

    reconciled = session.tasks.find_by_status("RECONCILED")
    assert len(reconciled) == 1
    assert reconciled[0]["reconciliation_status"] == "RECONCILED"

    # 验证产生了 account/reconciled 事件
    events = session.events.query("account/reconciled")
    assert len(events) >= 1
    payload = events[0]["payload"]
    assert payload["account_id"] == "a-stock-paper-1"
    assert payload["symbol"] == "600519.SH"


def test_reconciliation_mismatch_fails():
    """对账不一致（持仓为负）→ MISMATCH → FAILED。"""
    agent, session = _agent_and_session(signals=[_signal()])
    _drive_to_approved(session, agent)
    run_once(session, agent)  # 提交+对账

    # 模拟持仓为负，重新对账
    ADAPTER._positions = [Position(
        market=Market.A_SHARE, account_id="a-stock-paper-1",
        symbol="600519.SH", quantity=Decimal("-100"),
        available_quantity=Decimal("-100"),
        frozen_quantity=Decimal("0"), avg_cost=Decimal("10"),
        currency="CNY", as_of=datetime.now(UTC),
    )]
    from dsh_runtime.store import _get
    conn = _get()
    conn.execute(
        "UPDATE bot_tasks SET status = 'FILLED',"
        " reconciliation_status = 'PENDING'"
        " WHERE task_id LIKE '%sig-a-001'"
    )
    conn.commit()
    run_once(session, agent)
    failed = session.tasks.find_by_status("FAILED")
    assert len(failed) == 1
    assert failed[0]["reconciliation_status"] == "MISMATCH"


# ---- 8. 幂等：崩溃重启后不重复下单 ----

def test_crash_restart_no_duplicate_order():
    """提交后崩溃重启 → 通过查询恢复，不重复下单。"""
    agent, session = _agent_and_session(signals=[_signal()])
    _drive_to_approved(session, agent)
    run_once(session, agent)  # 提交

    submitted_count = len(ADAPTER.submitted)
    assert submitted_count == 1

    # 模拟崩溃：把任务拨回待执行态
    from dsh_runtime.store import _get
    conn = _get()
    conn.execute(
        "UPDATE bot_tasks SET status = 'APPROVED_SUBMITTING',"
        " order_id = NULL, reconciliation_status = 'PENDING'"
        " WHERE task_id LIKE '%sig-a-001'"
    )
    conn.commit()

    # 重启后第一个 tick：409 → 认领既有订单 → 查询恢复 → RECONCILED
    run_once(session, agent)
    # 不应该有第二笔提交
    assert len(ADAPTER.submitted) == submitted_count
