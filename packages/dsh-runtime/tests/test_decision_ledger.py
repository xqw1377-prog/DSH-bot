from pathlib import Path
from datetime import UTC, datetime, timedelta

from dsh_contracts import Market
from dsh_runtime import (
    BotIntelligenceJob,
    BotSession,
    TradeExecutionCore,
    classify_intel,
    load_profile,
    reset,
    run_once,
)

PROFILES = Path(__file__).resolve().parent.parent.parent.parent / "profiles"
NOW = datetime(2026, 8, 20, 0, 0, tzinfo=UTC)


class _Approvals:
    def request(self, **kwargs):
        return "appr-1"


class _Gateway:
    def __init__(self):
        self._positions = []
        self._accounts = [
            {
                "account_id": "paper-crypto-001",
                "cash": "1000",
                "equity": "1000",
                "reconciliation_version": "1",
            }
        ]
        self._order = None

    def get_health(self, market):
        return {"system_ok": True, "data_fresh": True, "market_session": "OPEN"}

    def get_signals(self, market):
        return [
            {
                "signal_id": "sig-ledger-1",
                "market": "CRYPTO",
                "strategy_id": "6celue-v5",
                "strategy_version": "v5",
                "symbol": "BTCUSDT",
                "side": "BUY",
                "strength": 0.95,
                "generated_at": NOW.isoformat(),
                "valid_until": (NOW + timedelta(minutes=15)).isoformat(),
                "evidence_refs": ["6celue:signals.jsonl:sig-ledger-1"],
                "quantity": "0.01",
                "entry_price": "67000",
            }
        ]

    def get_positions(self, market, account_id=None):
        return list(self._positions)

    def get_account_summary(self, market):
        return list(self._accounts)

    def preview_order(self, intent):
        return {
            "estimated_cost": "670",
            "estimated_slippage": "0",
            "risk": {
                "risk_snapshot_id": "rs-ledger-1",
                "position_after": "0.01",
                "risk_budget_delta": "670",
                "worst_case_loss": "67",
                "limits_hit": [],
            },
        }

    def register_risk_snapshot(self, market, snapshot):
        return {"ok": True, "risk_snapshot_id": snapshot["risk_snapshot_id"]}

    def get_approval(self, approval_id):
        return {
            "approval_id": approval_id,
            "status": "APPROVED",
            "evidence_refs": [
                "signal:sig-ledger-1",
                "strategy:6celue-v5@v5",
            ],
        }

    def request_order(self, intent):
        self._order = {
            "order_id": "ord-1",
            "status": "FILLED",
            "market": "CRYPTO",
            "symbol": intent.symbol,
            "side": intent.side.value,
            "filled_quantity": str(intent.quantity),
            "avg_price": "67000",
            "filled_at": NOW.isoformat(),
            "fees": "0",
            "taxes": "0",
            "fills": [{"quantity": str(intent.quantity), "price": "67000", "fee": "0"}],
        }
        self._positions = [
            {
                "symbol": intent.symbol,
                "quantity": str(intent.quantity),
                "available_quantity": str(intent.quantity),
                "frozen_quantity": "0",
            }
        ]
        self._accounts = [
            {
                "account_id": "paper-crypto-001",
                "cash": "330",
                "equity": "1000",
                "reconciliation_version": "2",
            }
        ]
        return {"order_id": "ord-1"}

    def get_order_status(self, market, order_id):
        return dict(self._order or {})


def test_classify_blocks_rumor_and_buy():
    grade, lane, approve = classify_intel(
        authority="blog",
        action="BUY",
        held=False,
        title="传闻即将上币",
        importance=0.9,
    )
    assert grade == "OBSERVE"
    assert lane == "OBSERVE"
    assert approve is True
    grade, lane, _ = classify_intel(
        authority="official",
        source_tier="PRIMARY",
        action="BUY",
        held=False,
        title="ETH 升级",
        event_type="GOVERNANCE",
        importance=0.8,
    )
    assert grade == "RISK_INCREASE"
    assert lane == "ADVICE"


def test_official_negative_held_writes_linked_ledger():
    reset()
    session = BotSession.for_profile(load_profile(PROFILES / "crypto-bot" / "profile.yaml"))
    job = BotIntelligenceJob(
        bot_name="crypto-bot",
        market="CRYPTO",
        source_env="DSH_MISSING_SOURCES",
        watchlist=("ETHUSDT",),
    )
    raw = {
        "title": "ETH 核心成员发布路线调整",
        "url": "https://example.com/eth",
        "published_at": "2026-08-20T00:00:00+00:00",
        "symbol": "ETHUSDT",
        "direction": "NEGATIVE",
        "confidence": 0.72,
        "event_id": "evt-eth-1",
        "event_type": "GOVERNANCE",
        "source_tier": "PRIMARY",
    }
    from dsh_runtime.intelligence import SourceSpec

    item = job._process_raw_item(
        session,
        spec=SourceSpec(source_id="eth-foundation", market="CRYPTO", authority="official"),
        raw=raw,
        symbols_held={"ETHUSDT"},
        marks={"ETHUSDT": "4200"},
    )
    assert item["action"] == "SELL"
    rows = session.ledger.list()
    assert len(rows) == 1
    row = rows[0]
    assert row["event_id"] == "evt-eth-1"
    assert row["intelligence_item_id"]
    assert row["task_id"]
    assert row["strategy_id"] == "intelligence-v1"
    assert row["risk_snapshot_id"]
    assert row["can_apply"] is False
    assert row["entry_plan"]["conditions"]
    assert row["exit_plan"]["invalidation"]
    coverage = session.ledger.coverage()
    assert coverage["decisions"] == 1
    assert coverage["fully_linked"] == 1
    reset()


def test_execution_chain_writes_approval_order_fill_and_reconcile():
    reset()
    session = BotSession.for_profile(load_profile(PROFILES / "crypto-bot" / "profile.yaml"))
    agent = TradeExecutionCore(
        name="crypto-bot",
        market=Market.CRYPTO,
        gateway=_Gateway(),
        approvals=_Approvals(),
        account_id="paper-crypto-001",
        mode="paper",
        now_fn=lambda: NOW,
    )
    run_once(session, agent)
    task = session.tasks.get("task-crypto-bot-sig-ledger-1")
    assert task is not None
    assert task["status"] == "AWAITING_APPROVAL"
    row = session.ledger.find_by_task(task["task_id"])
    assert row is not None
    assert row["approval_id"] == "appr-1"
    assert row["status"] == "AWAITING_APPROVAL"

    run_once(session, agent)
    row = session.ledger.find_by_task(task["task_id"])
    assert row is not None
    assert row["approval_id"] == "appr-1"
    assert row["order_id"] == "ord-1"
    assert row["fill_id"] == "ord-1#1"
    assert row["status"] == "RECONCILED"
    assert row["payload"]["reconciliation"]["reconciliation_status"] == "MATCHED"
    coverage = session.ledger.coverage()
    assert coverage["approved"] == 1
    assert coverage["ordered"] == 1
    assert coverage["filled"] == 1
    assert coverage["reconciled"] == 1
    reset()
