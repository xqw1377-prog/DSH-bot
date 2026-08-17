#!/usr/bin/env python3
"""Runtime 进程级冒烟：真实 uvicorn Gateway + 真实 Runtime Session + 插件链路。

验收项：
- Market Chief 经 Runtime 加载 Profile 并运行，产出 summary 记忆与事件
- Market Chief 在市场不可达时正确发 incident/opened（失败关闭）
- Incident Center 真实调用 Gateway emergency-stop（Kill Switch 闭环）
- 事件与记忆持久化到 SQLite，重启可读

用 DSH_ENV=development 开放鉴权便于本地冒烟；生产模式由 start-backends.sh 强制。
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for sub in (
    "packages/dsh-runtime/src",
    "packages/domain-contracts/src",
    "services/quant-gateway/src",
    "services/risk-policy/src",
    "plugins/dsh-quant-gateway/src",
    "plugins/dsh-trade-approval/src",
    "plugins/dsh-market-chief/src",
    "plugins/dsh-incident-center/src",
    "plugins/dsh-a-stock-agent/src",
):
    sys.path.insert(0, str(ROOT / sub))

os.environ["DSH_ENV"] = "development"
os.environ["DSH_RUNTIME_DB"] = str(ROOT / ".data" / "runtime-smoke.db")
os.makedirs(ROOT / ".data", exist_ok=True)
# 清掉旧 runtime db 以保证干净
db_path = ROOT / ".data" / "runtime-smoke.db"
if db_path.exists():
    db_path.unlink()

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import uvicorn
from dsh_contracts import (
    AccountSummary, HealthStatus, Market, OrderPreview, OrderSide,
    Position, RiskSnapshot, Signal, StrategyCandidate, StrategyStage,
)
from dsh_gateway_client import GatewayClient, RiskPolicyClient
from dsh_runtime import BotSession, Profile, load_profile, reset, run_once
from quant_gateway.adapters import register_adapter
from quant_gateway.adapters.base import MarketAdapter

PROFILES = ROOT / "profiles"


class SmokeAdapter(MarketAdapter):
    """可观测的假适配器：记录 emergency_stop 调用。"""

    calls: list[tuple] = []

    def __init__(self, market: Market, healthy: bool = True):
        self.market = market
        self._healthy = healthy

    def get_health(self) -> HealthStatus:
        return HealthStatus(
            market=self.market, system_ok=self._healthy, data_fresh=self._healthy,
            trading_channel_ok=self._healthy, clock_skew_ms=0,
            as_of=datetime.now(UTC),
        )

    def get_positions(self, account_id=None):
        return [Position(
            market=self.market, account_id="paper-1", symbol="BTC/USDT",
            quantity=Decimal("1"), available_quantity=Decimal("1"),
            frozen_quantity=Decimal("0"), avg_cost=Decimal("100"),
            currency="USDT", as_of=datetime.now(UTC),
        )]

    def get_account_summary(self):
        return [AccountSummary(
            market=self.market, account_id="paper-1",
            cash="10000", equity="10000", currency="USDT",
            reconciliation_version="v1", as_of=datetime.now(UTC),
        )]

    def get_signals(self):
        return [Signal(
            signal_id="sig-001", market=self.market,
            strategy_id="trend-1", strategy_version="1.0.0",
            symbol="BTC/USDT", side=OrderSide.BUY, strength=0.8,
            generated_at=datetime.now(UTC),
            valid_until=datetime.now(UTC) + timedelta(minutes=30),
            data_snapshot_id="snap-1",
        )]

    def preview_order(self, intent):
        return OrderPreview(
            intent=intent, estimated_cost="100", estimated_slippage="0.1",
            risk=RiskSnapshot(
                risk_snapshot_id="rs-1", market=self.market, account_id="paper-1",
                position_before=Decimal("0"), position_after=Decimal("1"),
                risk_budget_delta=Decimal("100"), worst_case_loss=Decimal("10"),
                as_of=datetime.now(UTC),
            ),
        )

    def request_order(self, intent):
        raise AssertionError("smoke 不应下单")

    def get_order_status(self, order_id):
        return {"order_id": order_id, "status": "FILLED"}

    def cancel_order(self, order_id):
        return {"order_id": order_id, "status": "CANCELLED"}

    def pause_strategy(self, strategy_id):
        pass

    def resume_strategy(self, strategy_id):
        pass

    def emergency_stop(self, account_id=None):
        type(self).calls.append(("emergency_stop", self.market.value, account_id))


class AStockSmokeAdapter(MarketAdapter):
    """A 股冒烟适配器：支持审批后执行闭环（下单→成交→对账）。

    模拟真实 venue：下单即成交，成交后回写持仓（T+1：available=0）。
    """

    def __init__(self):
        self.market = Market.A_SHARE
        self.submitted: list[dict] = []
        self._positions: list[Position] = []

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
        now = datetime.now(UTC)
        return [Signal(
            signal_id="sig-a-smoke", market=self.market,
            strategy_id="mean-reversion", strategy_version="1.0.0",
            symbol="600519.SH", side=OrderSide.BUY, strength=0.8,
            generated_at=now, valid_until=now + timedelta(minutes=30),
            data_snapshot_id="snap-a-smoke",
        )]

    def preview_order(self, intent):
        qty = Decimal(str(intent["quantity"])) if isinstance(intent, dict) else intent.quantity
        notional = qty * Decimal("10")
        return OrderPreview(
            intent=intent, estimated_cost=notional,
            estimated_slippage=Decimal("0.1"),
            risk=RiskSnapshot(
                risk_snapshot_id="rs-a-smoke", market=self.market,
                account_id="a-stock-paper-1",
                position_before=Decimal("0"), position_after=qty,
                risk_budget_delta=notional,
                worst_case_loss=notional * Decimal("0.1"),
                limits_hit=[], as_of=datetime.now(UTC),
            ),
        )

    def request_order(self, intent):
        payload = intent if isinstance(intent, dict) else intent.model_dump(mode="json")
        self.submitted.append(payload)
        order_id = f"A_SHARE-ord-{len(self.submitted)}"
        qty = Decimal(str(payload.get("quantity", "100")))
        symbol = payload.get("symbol", "600519.SH")
        # 成交后回写持仓（T+1：available=0）
        self._positions.append(Position(
            market=self.market, account_id="a-stock-paper-1",
            symbol=symbol, quantity=qty,
            available_quantity=Decimal("0"),  # T+1：今日买入冻结
            frozen_quantity=qty, avg_cost=Decimal("10"),
            currency="CNY", as_of=datetime.now(UTC),
        ))
        return order_id

    def get_order_status(self, order_id):
        return {
            "order_id": order_id, "status": "FILLED", "symbol": "600519.SH",
            "filled_quantity": "100", "avg_price": "10", "fees": "0",
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


def start_gateway() -> threading.Thread:
    register_adapter(Market.A_SHARE, SmokeAdapter(Market.A_SHARE, healthy=True))
    register_adapter(Market.CRYPTO, SmokeAdapter(Market.CRYPTO, healthy=True))
    config = uvicorn.Config(
        "quant_gateway.main:app", host="127.0.0.1", port=8011,
        log_level="warning",
    )
    server = uvicorn.Server(config)
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    # 等就绪
    import urllib.request
    for _ in range(40):
        try:
            urllib.request.urlopen("http://127.0.0.1:8011/healthz", timeout=1)
            return t
        except Exception:
            time.sleep(0.3)
    raise RuntimeError("gateway 未就绪")


def start_risk_policy() -> threading.Thread:
    """启动真实 risk-policy 服务：Kill Switch 的唯一可信事件源。"""
    config = uvicorn.Config(
        "risk_policy.main:app", host="127.0.0.1", port=8003,
        log_level="warning",
    )
    server = uvicorn.Server(config)
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    import urllib.request
    for _ in range(40):
        try:
            urllib.request.urlopen("http://127.0.0.1:8003/healthz", timeout=1)
            return t
        except Exception:
            time.sleep(0.3)
    raise RuntimeError("risk-policy 未就绪")


def main() -> int:
    reset()
    SmokeAdapter.calls.clear()
    start_gateway()
    start_risk_policy()
    gateway = GatewayClient(base_url="http://127.0.0.1:8011")
    risk_policy = RiskPolicyClient(base_url="http://127.0.0.1:8003")

    pass_n = 0
    fail_n = 0
    def check(name, cond, detail=""):
        nonlocal pass_n, fail_n
        if cond:
            print(f"  PASS: {name}")
            pass_n += 1
        else:
            print(f"  FAIL: {name} {detail}")
            fail_n += 1

    # ---- Market Chief 经 Runtime 运行 ----
    print("=== Market Chief 经 Runtime 加载 Profile 运行 ===")
    from dsh_market_chief import MarketChiefAgent
    chief = MarketChiefAgent(gateway=gateway)
    chief_profile = load_profile(PROFILES / "market-chief" / "profile.yaml")
    chief_session = BotSession.for_profile(chief_profile)
    run_once(chief_session, chief)

    summaries = chief_session.memory.recent(kind="market-summary")
    check("Market Chief 产出 market-summary 记忆", len(summaries) == 1,
          f"got {len(summaries)}")
    chief_events = chief_session.events.query("market/chief.summary")
    check("Market Chief 发出 market/chief.summary 事件", len(chief_events) == 1,
          f"got {len(chief_events)}")
    todos = chief_session.memory.recent(kind="todo")
    check("Market Chief 产出待办记忆", len(todos) >= 1)

    # ---- Market Chief 市场降级场景 ----
    print("=== Market Chief: 市场降级 → incident/opened ===")
    register_adapter(Market.CRYPTO, SmokeAdapter(Market.CRYPTO, healthy=False))
    run_once(chief_session, chief)
    incidents = chief_session.events.query("incident/opened")
    check("降级市场产生 incident/opened", len(incidents) >= 1,
          f"got {len(incidents)}")
    degraded_advice = chief_session.memory.recent(kind="advice")
    check("降级产生 advice 记忆", len(degraded_advice) >= 1)

    # ---- Incident Center 真实 Kill Switch（经 risk-policy 签发的 CRITICAL 事件）----
    print("=== Incident Center: risk-policy CRITICAL → 真实调 Gateway emergency_stop ===")
    from dsh_incident_center import IncidentCenter
    incident_center = IncidentCenter(gateway=gateway, risk_policy=risk_policy)
    inc_profile = Profile(
        name="incident-center", description="", market="GLOBAL",
        primary_tools=frozenset({"incident_alert"}),
        prohibited=frozenset(),
    )
    inc_session = BotSession.for_profile(inc_profile)
    # 向 risk-policy 上报一条 CRITICAL 规则违反（持仓超限）
    risk_policy.report_violation({
        "severity": "CRITICAL",
        "rule_id": "MAX_POSITION_RATIO",
        "market": "CRYPTO",
        "measured": 0.42,
        "limit": 0.30,
        "account_id": "paper-1",
        "evidence_refs": ["position:btc-usdt"],
    })
    # 文本事故只产生告警，不触发 Kill Switch
    inc_session.events.emit(
        "incident/opened", "CRYPTO", "bot", "crypto-bot",
        {"reason": "position breach emergency: 持仓超限"},
    )
    run_once(inc_session, incident_center)
    check("Incident Center 调用 Gateway emergency_stop",
          any(c[0] == "emergency_stop" and c[1] == "CRYPTO" for c in SmokeAdapter.calls),
          f"calls={SmokeAdapter.calls}")
    mitigated = inc_session.events.query("incident/mitigated")
    check("Incident Center 发出 incident/mitigated", len(mitigated) >= 1,
          f"got {len(mitigated)}")
    mit_mem = inc_session.memory.recent(kind="mitigation")
    check("Kill Switch 留痕记忆", len(mit_mem) >= 1)
    # Kill Switch 专用事件
    ks_requested = inc_session.events.query("kill_switch/requested")
    check("kill_switch/requested 事件", len(ks_requested) >= 1)
    ks_succeeded = inc_session.events.query("kill_switch/succeeded")
    check("kill_switch/succeeded 事件", len(ks_succeeded) >= 1)

    # ---- 文本事故不触发 Kill Switch（只告警）----
    print("=== Incident Center: 文本事故 → 只告警，不触发 Kill Switch ===")
    before = len(SmokeAdapter.calls)
    inc_session.events.emit(
        "incident/opened", "A_SHARE", "bot", "a-stock-bot",
        {"reason": "market data degraded"},
    )
    run_once(inc_session, incident_center)
    check("文本事故不调 emergency_stop", len(SmokeAdapter.calls) == before,
          f"calls={SmokeAdapter.calls}")

    # ---- 去抖：HALTED 后第二个 CRITICAL 不重复触发 ----
    print("=== Incident Center: 去抖 — HALTED 后 CRITICAL 不重复触发 ===")
    before2 = len(SmokeAdapter.calls)
    # 再上报一条 CRITICAL（不同 rule_id，同 market）
    risk_policy.report_violation({
        "severity": "CRITICAL",
        "rule_id": "MAX_DRAWDOWN",
        "market": "CRYPTO",
        "measured": 0.15,
        "limit": 0.10,
        "account_id": "paper-1",
    })
    run_once(inc_session, incident_center)
    check("HALTED 市场不重复 emergency_stop", len(SmokeAdapter.calls) == before2,
          f"calls={SmokeAdapter.calls}")
    skipped = inc_session.memory.recent(kind="kill-switch-skipped")
    check("去抖产生 skipped 记忆", len(skipped) >= 1)

    # ---- 人工恢复（需审批ID与操作人）----
    print("=== Incident Center: 人工恢复 HALTED 市场 ===")
    check("CRYPTO 处于 HALTED", incident_center._is_halted("CRYPTO"))
    # 恢复必须有审批ID与操作人
    resume_rec = incident_center.resume_market(
        inc_session, "CRYPTO",
        resumed_by="risk-officer-alice",
        approval_id="appr-resume-001",
        reason="position rebalanced, risk within limits",
    )
    check("resume_market 返回恢复记录", resume_rec is not None)
    check("恢复记录含审批ID", resume_rec and resume_rec.get("resume_approval_id") == "appr-resume-001")
    check("CRYPTO 不再 HALTED", not incident_center._is_halted("CRYPTO"))
    resume_mem = inc_session.memory.recent(kind="manual-resume")
    check("人工恢复留痕", len(resume_mem) >= 1)

    # ---- A 股插件: Paper 信号 → 预览 → 审批 → 执行 → 对账 ----
    print("=== A 股插件: Paper 信号 → 预览 → 审批 → 执行 → 对账 ===")
    # 注册支持执行闭环的 A 股适配器（替代默认 SmokeAdapter）
    a_stock_adapter = AStockSmokeAdapter()
    register_adapter(Market.A_SHARE, a_stock_adapter)
    # 冒烟测试可能不在 A 股交易时段，mock 为 True
    import dsh_a_stock_agent.agent as _a_stock_mod
    _a_stock_mod._is_trading_hours = lambda dt: True

    from dsh_trade_approval import ApprovalWorkflow
    from dsh_a_stock_agent import AStockAgent
    approvals = ApprovalWorkflow(gateway_base_url="http://127.0.0.1:8011")
    a_stock = AStockAgent(
        gateway=gateway, approvals=approvals,
        account_id="a-stock-paper-1",
    )
    a_stock_profile = load_profile(PROFILES / "a-stock-bot" / "profile.yaml")
    a_stock_session = BotSession.for_profile(a_stock_profile)
    run_once(a_stock_session, a_stock)

    # 验证审批请求已创建
    import urllib.request
    appr_resp = urllib.request.urlopen(
        "http://127.0.0.1:8011/v1/approvals?status=REQUESTED", timeout=5
    )
    appr_list = __import__("json").loads(appr_resp.read())
    a_stock_apprs = [a for a in appr_list if a.get("market") == "A_SHARE"]
    check("A 股信号生成审批请求", len(a_stock_apprs) >= 1,
          f"got {len(a_stock_apprs)}")
    # 第二 tick 不重复
    run_once(a_stock_session, a_stock)
    appr_resp2 = urllib.request.urlopen(
        "http://127.0.0.1:8011/v1/approvals?status=REQUESTED", timeout=5
    )
    appr_list2 = __import__("json").loads(appr_resp2.read())
    a_stock_apprs2 = [a for a in appr_list2 if a.get("market") == "A_SHARE"]
    check("第二 tick 不重复审批", len(a_stock_apprs2) == len(a_stock_apprs),
          f"{len(a_stock_apprs)} -> {len(a_stock_apprs2)}")

    # ---- 审批通过 → 执行闭环 ----
    approval_id = a_stock_apprs[0]["approval_id"]
    decide_resp = urllib.request.urlopen(
        urllib.request.Request(
            f"http://127.0.0.1:8011/v1/approvals/{approval_id}/decide",
            data=__import__("json").dumps({
                "decision": "APPROVED", "decided_by": "risk-officer",
            }).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        ),
        timeout=5,
    )
    check("审批通过返回 200", decide_resp.status == 200,
          f"status={decide_resp.status}")

    # 执行 tick：审批通过 → 提交 → 成交 → 对账
    run_once(a_stock_session, a_stock)
    run_once(a_stock_session, a_stock)  # 确保推进到 RECONCILED

    check("A 股订单已提交", len(a_stock_adapter.submitted) == 1,
          f"submitted={len(a_stock_adapter.submitted)}")
    reconciled = a_stock_session.tasks.find_by_status("RECONCILED")
    check("A 股任务达到 RECONCILED", len(reconciled) == 1,
          f"reconciled={len(reconciled)}")
    if reconciled:
        check("对账状态为 RECONCILED",
              reconciled[0]["reconciliation_status"] == "RECONCILED",
              f"recon={reconciled[0]['reconciliation_status']}")
    # 验证 order/submitted 和 account/reconciled 事件
    order_events = a_stock_session.events.query("order/submitted")
    check("产生 order/submitted 事件", len(order_events) >= 1,
          f"got {len(order_events)}")
    recon_events = a_stock_session.events.query("account/reconciled")
    check("产生 account/reconciled 事件", len(recon_events) >= 1,
          f"got {len(recon_events)}")

    # ---- 持久化：重启 Session 后记忆可读 ----
    print("=== 持久化：Session 重启后记忆可读 ===")
    reset()
    chief_session2 = BotSession.for_profile(chief_profile)
    # 新 session 连同一 DB，历史记忆应可查
    hist = chief_session2.memory.recent(kind="market-summary", limit=100)
    check("重启后历史 market-summary 可读", len(hist) >= 1,
          f"got {len(hist)}")

    print(f"\n===== 冒烟结果: PASS={pass_n} FAIL={fail_n} =====")
    return 0 if fail_n == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
