"""projection-api 代理测试:用 MockTransport 模拟上游,验证路径与参数透传、
上游状态码原样传递(失败关闭不被压成 500)。"""

from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient

import projection_api.main as projection_main
from projection_api.main import app

client = TestClient(app)

_SESSION_OPEN = datetime(2026, 8, 18, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
_SESSION_CLOSED = datetime(2026, 8, 18, 20, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


@pytest.fixture()
def mock_upstream(monkeypatch):
    holder = {"handler": None}
    real_async_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        def delegate(request: httpx.Request) -> httpx.Response:
            return holder["handler"](request)

        kwargs["transport"] = httpx.MockTransport(delegate)
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(projection_main.httpx, "AsyncClient", factory)
    return holder


def test_positions_proxied(mock_upstream):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json=[{"symbol": "600519.SH"}])

    mock_upstream["handler"] = handler
    result = client.get("/v1/markets/A_SHARE/positions").json()
    assert result == [{"symbol": "600519.SH"}]
    assert seen["url"].endswith("/v1/markets/A_SHARE/positions")


def test_upstream_failure_status_preserved(mock_upstream):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "failing closed"})

    mock_upstream["handler"] = handler
    resp = client.get("/v1/markets/A_SHARE/positions")
    assert resp.status_code == 503


def test_unreachable_upstream_is_503(mock_upstream):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    mock_upstream["handler"] = handler
    assert client.get("/v1/markets/A_SHARE/positions").status_code == 503


def test_approvals_params_forwarded(mock_upstream):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json=[])

    mock_upstream["handler"] = handler
    client.get("/v1/approvals", params={"status": "REQUESTED", "market": "A_SHARE"})
    assert "status=REQUESTED" in seen["url"]
    assert "market=A_SHARE" in seen["url"]


def test_chief_refuses_action_verbs(tmp_path, monkeypatch):
    monkeypatch.setenv("DSH_RUNTIME_DB", str(tmp_path / "missing.db"))
    resp = client.post("/v1/chief/query", json={"question": "请你立刻批准这笔订单"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["refused"] is True
    assert "不能执行" in body["text"]


def test_chief_includes_health_context(tmp_path, monkeypatch):
    monkeypatch.setenv("DSH_RUNTIME_DB", str(tmp_path / "missing.db"))

    def fake_get(url, headers=None, timeout=2.0):
        market = "CRYPTO" if "CRYPTO" in url else "A_SHARE"
        return type(
            "R",
            (),
            {
                "is_success": True,
                "status_code": 200,
                "json": lambda self: {
                    "system_ok": True,
                    "data_fresh": market == "CRYPTO",
                },
            },
        )()

    monkeypatch.setattr(projection_main.httpx, "get", fake_get)
    resp = client.post("/v1/chief/query", json={"question": "现在系统健康吗"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["refused"] is False
    assert "CRYPTO 正常" in body["text"]
    assert "A_SHARE 降级" in body["text"]
    assert "不含资金动作" in body["text"]


def test_incidents_empty_without_runtime_db(monkeypatch):
    monkeypatch.delenv("DSH_RUNTIME_DB", raising=False)

    def fake_get(url, params=None, headers=None, timeout=2.0):
        return type("R", (), {"is_success": True, "json": lambda self: []})()

    monkeypatch.setattr(projection_main.httpx, "get", fake_get)
    assert client.get("/v1/incidents").json() == []


def test_incidents_include_gateway_kill_switch_audit(monkeypatch):
    monkeypatch.delenv("DSH_RUNTIME_DB", raising=False)

    def fake_get(url, params=None, headers=None, timeout=2.0):
        assert "/v1/audit" in url
        return type(
            "R",
            (),
            {
                "is_success": True,
                "json": lambda self: [
                    {
                        "audit_id": "audit-ks-1",
                        "occurred_at": "2026-08-17T00:00:00+00:00",
                        "actor": "alice",
                        "action": "kill_switch.succeeded",
                        "market": "CRYPTO",
                        "subject_id": "paper-crypto-001",
                        "outcome": "OK",
                        "detail": "emergency_stop engaged",
                    }
                ],
            },
        )()

    monkeypatch.setattr(projection_main.httpx, "get", fake_get)
    rows = client.get("/v1/incidents").json()
    assert len(rows) == 1
    assert rows[0]["event_type"] == "kill_switch/succeeded"
    assert rows[0]["source"] == "gateway-audit"


def test_experiments_proxied_to_evolution(mock_upstream):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json=[])

    mock_upstream["handler"] = handler
    client.get("/v1/experiments")
    assert ":8002" in seen["url"]


def _health(fresh: bool, **extra):
    body = {
        "system_ok": extra.get("system_ok", True),
        "data_fresh": fresh,
        "trading_channel_ok": extra.get("trading_channel_ok", True),
        "clock_skew_ms": extra.get("clock_skew_ms", 3),
        "degraded": extra.get("degraded", False),
        "detail": extra.get("detail", "ok"),
        "as_of": "2026-08-18T00:00:00+00:00",
    }
    return body


def test_bots_overview_three_cards_and_mixed_mode(monkeypatch):
    monkeypatch.setenv("DSH_CRYPTO_MODE", "paper")
    monkeypatch.setenv("DSH_A_SHARE_MODE", "shadow")
    monkeypatch.setattr(projection_main, "get_bot_tasks", lambda: [])
    monkeypatch.setattr(projection_main, "get_incidents", lambda limit=50: [])

    def fetch(path: str):
        if "CRYPTO/health" in path:
            return _health(True)
        if "A_SHARE/health" in path:
            return _health(True)
        if "approvals" in path:
            return [{"status": "REQUESTED", "market": "CRYPTO"}]
        return None

    from projection_api.overview import build_overview

    body = build_overview(fetch, now=_SESSION_OPEN)
    ids = [b["bot_id"] for b in body["bots"]]
    assert ids == ["market-chief", "crypto", "a-share"]
    assert body["global_mode"] == "MIXED"
    assert body["live_anomaly"] is False
    chief = body["bots"][0]
    assert chief["read_only"] is True
    assert chief["order"] == "NONE"
    crypto = body["bots"][1]
    assert crypto["mode"] == "PAPER"
    assert crypto["counts"]["pending_approvals"] == 1
    assert body["bots"][2]["mode"] == "SHADOW"


def test_bots_overview_stale_not_fresh(monkeypatch):
    monkeypatch.setattr(projection_main, "get_bot_tasks", lambda: [])
    monkeypatch.setattr(projection_main, "get_incidents", lambda limit=50: [])

    def fetch(path: str):
        if "CRYPTO/health" in path:
            return _health(False)
        if "A_SHARE/health" in path:
            return _health(True)
        return []

    from projection_api.overview import build_overview

    body = build_overview(fetch, now=_SESSION_OPEN)
    crypto = next(b for b in body["bots"] if b["bot_id"] == "crypto")
    assert crypto["data"] == "STALE"
    assert crypto["data"] != "FRESH"
    assert any("STALE" in a for a in body["alerts"])


def test_bots_overview_halted_and_unknown_alert(monkeypatch):
    monkeypatch.setattr(
        projection_main,
        "get_bot_tasks",
        lambda: [
            {
                "bot": "crypto-bot",
                "status": "SUBMISSION_UNKNOWN",
                "reconciliation_status": "PENDING",
            }
        ],
    )
    monkeypatch.setattr(
        projection_main,
        "get_incidents",
        lambda limit=50: [
            {
                "event_type": "kill_switch/succeeded",
                "market": "CRYPTO",
                "occurred_at": "2026-08-18T00:00:00+00:00",
            }
        ],
    )

    def fetch(path: str):
        if "CRYPTO/health" in path:
            return _health(
                True,
                system_ok=False,
                trading_channel_ok=False,
                detail="emergency stop engaged",
            )
        if "A_SHARE/health" in path:
            return _health(True)
        return []

    from projection_api.overview import build_overview

    body = build_overview(fetch, now=_SESSION_OPEN)
    crypto = next(b for b in body["bots"] if b["bot_id"] == "crypto")
    assert crypto["risk"] == "HALTED"
    assert crypto["order"] == "UNKNOWN"
    assert any("HALTED" in a for a in body["alerts"])
    assert any("UNKNOWN" in a for a in body["alerts"])


def test_bots_overview_live_is_anomaly_not_global_live(monkeypatch):
    monkeypatch.setenv("DSH_CRYPTO_MODE", "live")
    monkeypatch.setenv("DSH_A_SHARE_MODE", "paper")
    monkeypatch.setattr(projection_main, "get_bot_tasks", lambda: [])
    monkeypatch.setattr(projection_main, "get_incidents", lambda limit=50: [])

    def fetch(path: str):
        return _health(True) if "health" in path else []

    from projection_api.overview import build_overview

    body = build_overview(fetch, now=_SESSION_OPEN)
    assert body["live_anomaly"] is True
    assert body["global_mode"] == "SECURITY_VIOLATION"
    assert any("SECURITY VIOLATION" in a for a in body["alerts"])


def test_bots_overview_missing_mode_is_unknown_not_paper(monkeypatch):
    monkeypatch.delenv("DSH_CRYPTO_MODE", raising=False)
    monkeypatch.delenv("DSH_A_SHARE_MODE", raising=False)
    monkeypatch.setattr(projection_main, "get_bot_tasks", lambda: [])
    monkeypatch.setattr(projection_main, "get_incidents", lambda limit=50: [])

    def fetch(path: str):
        return _health(True) if "health" in path else []

    from projection_api.overview import build_overview

    body = build_overview(fetch, now=_SESSION_OPEN)
    assert body["global_mode"] == "UNKNOWN"
    crypto = next(b for b in body["bots"] if b["bot_id"] == "crypto")
    assert crypto["mode"] == "UNKNOWN"
    assert crypto["mode"] != "PAPER"


def test_bots_overview_a_share_closed_is_market_closed_not_stale(monkeypatch):
    monkeypatch.setenv("DSH_CRYPTO_MODE", "paper")
    monkeypatch.setenv("DSH_A_SHARE_MODE", "paper")
    monkeypatch.setattr(projection_main, "get_bot_tasks", lambda: [])
    monkeypatch.setattr(projection_main, "get_incidents", lambda limit=50: [])

    def fetch(path: str):
        if "CRYPTO/health" in path:
            return _health(True)
        if "A_SHARE/health" in path:
            return _health(False)
        return []

    from projection_api.overview import build_overview

    body = build_overview(fetch, now=_SESSION_CLOSED)
    ashare = next(b for b in body["bots"] if b["bot_id"] == "a-share")
    assert ashare["data"] == "MARKET_CLOSED"
    assert ashare["data"] != "STALE"
    assert not any("A 股" in a and "STALE" in a for a in body["alerts"])


def test_bots_overview_partial_crypto_failure_keeps_other_cards(monkeypatch):
    monkeypatch.setenv("DSH_CRYPTO_MODE", "paper")
    monkeypatch.setenv("DSH_A_SHARE_MODE", "paper")
    monkeypatch.setattr(projection_main, "get_bot_tasks", lambda: [])
    monkeypatch.setattr(projection_main, "get_incidents", lambda limit=50: [])

    def fetch(path: str):
        if "CRYPTO" in path:
            raise RuntimeError("crypto unreachable")
        if "A_SHARE/health" in path:
            return _health(True)
        return []

    from projection_api.overview import build_overview

    body = build_overview(fetch, now=_SESSION_OPEN)
    ids = [b["bot_id"] for b in body["bots"]]
    assert ids == ["market-chief", "crypto", "a-share"]
    crypto = next(b for b in body["bots"] if b["bot_id"] == "crypto")
    ashare = next(b for b in body["bots"] if b["bot_id"] == "a-share")
    chief = next(b for b in body["bots"] if b["bot_id"] == "market-chief")
    assert crypto["runtime"] == "OFFLINE"
    assert crypto["data"] == "DISCONNECTED"
    assert ashare["runtime"] == "ONLINE"
    assert ashare["data"] == "FRESH"
    assert chief["runtime"] == "DEGRADED"
    assert chief["data"] != "STALE"


def test_bots_overview_halted_outranks_healthy_peer(monkeypatch):
    monkeypatch.setenv("DSH_CRYPTO_MODE", "paper")
    monkeypatch.setenv("DSH_A_SHARE_MODE", "paper")
    monkeypatch.setattr(projection_main, "get_bot_tasks", lambda: [])
    monkeypatch.setattr(
        projection_main,
        "get_incidents",
        lambda limit=50: [
            {
                "event_type": "kill_switch/succeeded",
                "market": "CRYPTO",
                "occurred_at": "2026-08-18T00:00:00+00:00",
            }
        ],
    )

    def fetch(path: str):
        return _health(True) if "health" in path else []

    from projection_api.overview import pick_severity, build_overview

    assert pick_severity("NORMAL", "HALTED", "WARNING") == "HALTED"
    body = build_overview(fetch, now=_SESSION_OPEN)
    crypto = next(b for b in body["bots"] if b["bot_id"] == "crypto")
    ashare = next(b for b in body["bots"] if b["bot_id"] == "a-share")
    chief = next(b for b in body["bots"] if b["bot_id"] == "market-chief")
    assert crypto["risk"] == "HALTED"
    assert ashare["risk"] == "NORMAL"
    assert chief["risk"] == "HALTED"
    assert chief["severity"] == "HALTED"


def test_overview_requires_service_identity_in_production(monkeypatch):
    monkeypatch.setenv("DSH_ENV", "production")
    monkeypatch.delenv("PROJECTION_API_KEY", raising=False)
    monkeypatch.delenv("PROJECTION_API_KEYS", raising=False)
    monkeypatch.setattr(projection_main, "get_bot_tasks", lambda: [])
    monkeypatch.setattr(projection_main, "get_incidents", lambda limit=50: [])
    resp = client.get("/v1/bots/overview")
    assert resp.status_code == 503

    monkeypatch.setenv("PROJECTION_API_KEY", "proj-read")
    denied = client.get("/v1/bots/overview")
    assert denied.status_code == 401
    ok = client.get("/v1/bots/overview", headers={"X-API-Key": "proj-read"})
    assert ok.status_code == 200
    assert [b["bot_id"] for b in ok.json()["bots"]] == [
        "market-chief",
        "crypto",
        "a-share",
    ]

