from decimal import Decimal

from dsh_runtime.reconcile import evaluate_reconcile, filled_quantity_from_venue


def test_filled_quantity_sums_fills():
    venue = {"fills": [{"quantity": "0.004"}, {"qty": "0.006"}], "filled_quantity": "9"}
    assert filled_quantity_from_venue(venue) == Decimal("0.01")


def test_buy_matched_when_position_and_cash_follow_fill():
    verdict = evaluate_reconcile(
        side="BUY",
        baseline_position="0.35",
        baseline_cash="58403",
        venue={"filled_quantity": "0.01", "avg_price": "67420", "fees": "0"},
        position={
            "quantity": "0.36",
            "available_quantity": "0.36",
            "frozen_quantity": "0",
        },
        account={"cash": "57728.8", "equity": "1", "reconciliation_version": "v1"},
    )
    assert verdict.matched, verdict.reasons


def test_old_position_without_cash_move_is_mismatch():
    verdict = evaluate_reconcile(
        side="BUY",
        baseline_position="0.35",
        baseline_cash="58403",
        venue={"filled_quantity": "0.01", "avg_price": "67420", "fees": "1.5"},
        position={
            "quantity": "0.35",
            "available_quantity": "0.35",
            "frozen_quantity": "0",
        },
        account={"cash": "58403", "equity": "82000"},
    )
    assert not verdict.matched
    assert any("position" in r for r in verdict.reasons)
    assert any("cash" in r for r in verdict.reasons)


def test_available_plus_frozen_must_equal_quantity():
    verdict = evaluate_reconcile(
        side="SELL",
        baseline_position="1",
        baseline_cash="100",
        venue={"filled_quantity": "0.2", "avg_price": "10", "fees": "0"},
        position={
            "quantity": "0.8",
            "available_quantity": "0.5",
            "frozen_quantity": "0.1",
        },
        account={"cash": "102"},
    )
    assert not verdict.matched
    assert any("available" in r for r in verdict.reasons)
