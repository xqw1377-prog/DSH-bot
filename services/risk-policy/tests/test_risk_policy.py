from fastapi.testclient import TestClient

from risk_policy.main import app

client = TestClient(app)


def _check(**overrides) -> dict:
    payload = {
        "market": "A_SHARE",
        "account_id": "acc-1",
        "symbol": "600519.SH",
        "quantity": "100",
        "notional": "10000",
        "worst_case_loss": "500",
        "equity": "1000000",
    }
    payload.update(overrides)
    resp = client.post("/v1/check-order", json=payload)
    assert resp.status_code == 200
    return resp.json()


def test_healthz():
    assert client.get("/healthz").json()["status"] == "ok"


def test_budgets_listed():
    budgets = client.get("/v1/risk-budget").json()
    assert {b["market"] for b in budgets} == {"A_SHARE", "CRYPTO"}


def test_check_passes_within_budget():
    result = _check()
    assert result["passed"] is True
    assert result["limits_hit"] == []


def test_notional_over_max_position_fails():
    result = _check(notional="1000000")
    assert result["passed"] is False
    assert any(l.startswith("max_position") for l in result["limits_hit"])


def test_loss_ratio_over_limit_fails():
    # 默认 A 股单笔最坏损失上限为权益的 1%
    result = _check(worst_case_loss="20000", equity="1000000")
    assert result["passed"] is False
    assert any("max_loss_ratio" in l for l in result["limits_hit"])


def test_missing_equity_fails_closed():
    # 数据不可用 ≠ 真实风险事件：失败关闭拒单，但不触发 Kill Switch
    result = _check(equity="0")
    assert result["passed"] is False
    assert "equity_unavailable" in result["limits_hit"]
    assert result["severity"] == "DATA_UNAVAILABLE"
    assert result["kill_switch"] is False


def test_max_position_is_high_not_kill_switch():
    result = _check(notional="1000000")
    assert result["passed"] is False
    assert result["severity"] == "HIGH"
    assert result["kill_switch"] is False


def test_loss_ratio_is_critical_kill_switch():
    result = _check(worst_case_loss="20000", equity="1000000")
    assert result["severity"] == "CRITICAL"
    assert result["kill_switch"] is True
