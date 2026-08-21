from projection_api.trade_quality import build_trade_quality_report


def test_report_scores_signal_noise_and_blocks_apply():
    report = build_trade_quality_report(
        decisions=[
            {
                "task_id": "w1",
                "market": "CRYPTO",
                "symbol": "HYPEUSDT",
                "action": "WATCH",
                "skip_reason": "WAITING_CONFIRM",
            },
            {
                "task_id": "e1",
                "market": "CRYPTO",
                "symbol": "SOLUSDT",
                "action": "ABANDON",
                "skip_reason": "SIGNAL_EXPIRED",
            },
            {
                "task_id": "s1",
                "market": "CRYPTO",
                "symbol": "ETHUSDT",
                "action": "SELL",
                "suggested_price": "4000",
                "outcome_price": "3900",
                "simulated_pnl": "1.0",
                "strength": 0.9,
                "worst_case_loss": "40",
            },
            {
                "task_id": "b1",
                "market": "CRYPTO",
                "symbol": "BTCUSDT",
                "action": "BUY",
                "suggested_price": "68000",
                "outcome_price": "67000",
                "simulated_pnl": "-10",
                "strength": 0.8,
                "worst_case_loss": "80",
            },
        ],
        crypto_account={"equity": "2395", "cash": "2100"},
        ashare_account={"equity": "1000000", "cash": "790000"},
        crypto_positions=[{"symbol": "ETHUSDT", "quantity": "0.1"}],
        as_of="2026-08-20T00:00:00+00:00",
    )
    assert report["disclaimer"].startswith("只审计")
    assert report["trade_blocked"] is True
    assert report["score"]["overall"] is not None
    assert report["score"]["dimensions"]["exit"]["available"] is False
    assert report["coverage"]["mae_mfe"] is False
    assert report["coverage"]["crypto_fills"] is False
    assert report["best"][0]["symbol"] == "ETHUSDT"
    assert report["worst"][0]["symbol"] == "BTCUSDT"
    assert all(item["can_apply"] is False for item in report["suggestions"])
    assert all(item["stage"] == "SUGGESTION" for item in report["suggestions"])
    assert any(item["suggestion_id"] == "watch-not-sell" for item in report["suggestions"])
    assert any(item["suggestion_id"] == "need-crypto-fills" for item in report["suggestions"])


def test_report_uses_closed_trades_for_exit_and_fees():
    report = build_trade_quality_report(
        decisions=[],
        crypto_account={"equity": "2395", "cash": "400"},
        closed_trades=[
            {
                "symbol": "ETHUSDT",
                "side": "SELL",
                "entry_price": "2400",
                "exit_price": "2300",
                "pnl": "10",
                "fee": "0.4",
                "exit_reason": "移动止盈",
                "source_trade_id": "t-win",
            },
            {
                "symbol": "SOLUSDT",
                "side": "BUY",
                "entry_price": "180",
                "exit_price": "170",
                "pnl": "-8",
                "fee": "0.3",
                "exit_reason": "固定止损",
                "source_trade_id": "t-lose",
            },
        ],
        equity_curve=[
            {"ts": "2026-08-19T00:00:00+00:00", "equity": "2000"},
            {"ts": "2026-08-20T00:00:00+00:00", "equity": "2395"},
        ],
    )
    assert report["coverage"]["crypto_fills"] is True
    assert report["coverage"]["fees_slippage"] is True
    assert report["coverage"]["exit_reasons"] is True
    assert report["coverage"]["daily_equity_curve"] is True
    assert report["coverage"]["mae_mfe"] is False
    assert report["coverage"]["ledger_filled"] is False
    with_pipeline = build_trade_quality_report(
        decisions=[],
        closed_trades=[{"symbol": "ETHUSDT", "pnl": "10", "fee": "0.1", "exit_reason": "x"}],
        pipeline_candidates=[
            {
                "suggestion_id": "replay-stop-1r",
                "stage": "SHADOW",
                "can_apply": False,
                "trade_blocked": True,
            }
        ],
    )
    assert any(item["suggestion_id"] == "replay-stop-1r" for item in with_pipeline["suggestions"])
    assert all(item["can_apply"] is False for item in with_pipeline["suggestions"])
    report_linked = build_trade_quality_report(
        decisions=[],
        closed_trades=[{"symbol": "ETHUSDT", "pnl": "10", "fee": "0.1", "exit_reason": "x"}],
        ledger_coverage={"filled": 1, "audited": 1},
    )
    assert report_linked["coverage"]["ledger_filled"] is True
    assert report_linked["coverage"]["ledger_audited"] is True
    assert report["score"]["dimensions"]["exit"]["available"] is True
    assert report["score"]["dimensions"]["execution"]["available"] is True
    assert report["best"][0]["symbol"] == "ETHUSDT"
    assert report["worst"][0]["symbol"] == "SOLUSDT"
    assert all(item["can_apply"] is False for item in report["suggestions"])
    assert any(item["suggestion_id"] == "review-fees" for item in report["suggestions"])


def test_empty_report_does_not_invent_scores():
    report = build_trade_quality_report(decisions=[], crypto_account=None)
    assert report["score"]["overall"] is None or report["counts"]["decisions"] == 0
    assert report["score"]["dimensions"]["signal"]["available"] is False
    assert report["best"] == []
    assert report["worst"] == []


def test_report_prefers_ledger_rows_when_available():
    report = build_trade_quality_report(
        decisions=[],
        ledger_rows=[
            {
                "decision_id": "dec-1",
                "market": "CRYPTO",
                "symbol": "ETHUSDT",
                "status": "RECONCILED",
                "approval_id": "appr-1",
                "order_id": "ord-1",
                "fill_id": "fill-1",
                "audit_id": "aud-1",
                "payload": {
                    "venue_fill": {
                        "market": "CRYPTO",
                        "symbol": "ETHUSDT",
                        "filled_quantity": "0.1",
                        "avg_price": "2400",
                        "fees": "0.2",
                    },
                    "trade": {
                        "symbol": "ETHUSDT",
                        "side": "SELL",
                        "entry_price": "2400",
                        "exit_price": "2300",
                        "pnl": "10",
                        "fee": "0.2",
                        "exit_reason": "移动止盈",
                        "source_trade_id": "life-1",
                    },
                    "reconciliation": {"reconciliation_status": "MATCHED"},
                },
            }
        ],
        ledger_coverage={
            "approved": 1,
            "ordered": 1,
            "filled": 1,
            "reconciled": 1,
            "audited": 1,
        },
    )
    assert report["coverage"]["ledger_approved"] is True
    assert report["coverage"]["ledger_ordered"] is True
    assert report["coverage"]["ledger_reconciled"] is True
    assert report["coverage"]["ledger_audited"] is True
    assert report["coverage"]["crypto_fills"] is True
    assert report["counts"]["closed_trades"] == 1
    assert report["best"][0]["symbol"] == "ETHUSDT"
    assert not any(item["suggestion_id"] == "need-crypto-fills" for item in report["suggestions"])
