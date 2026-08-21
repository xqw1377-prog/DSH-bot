"""内部服务鉴权验收:五种状态全覆盖(以 risk-policy 为模板,逐服务 spot check)。

规则(projection-api 同款):
- 开发模式 + 未配置密钥 → 放行(本地联调)
- 生产模式 + 未配置密钥 → 503 失败关闭
- 配置密钥 + 错误/缺失 key → 401
- 配置密钥 + 正确 key → 200
- /healthz 永远开放(探针)
"""

import importlib

import pytest
from fastapi.testclient import TestClient

SERVICES = [
    # (module, app_attr, keys_env, probe_path)
    ("risk_policy.main", "app", "RISK_POLICY_API_KEYS", "/v1/risk-budget"),
    ("risk_auditor.main", "app", "RISK_AUDITOR_API_KEYS", "/v1/conclusions/x"),
    ("incident_center.main", "app", "INCIDENT_CENTER_API_KEYS", "/v1/incidents"),
    ("intelligence_ingest.main", "app", "INTELLIGENCE_INGEST_API_KEYS", "/v1/sources"),
]


@pytest.mark.parametrize("module,app_attr,keys_env,probe", SERVICES)
def test_service_auth_matrix(module, app_attr, keys_env, probe, monkeypatch):
    app = getattr(importlib.import_module(module), app_attr)
    client = TestClient(app)

    # healthz 永远开放
    monkeypatch.delenv(keys_env, raising=False)
    monkeypatch.setenv("DSH_ENV", "production")
    assert client.get("/healthz").status_code == 200

    # 生产 + 未配置 → 503 失败关闭
    assert client.get(probe).status_code == 503

    # 配置后:错 key → 401,对 key → 通过
    monkeypatch.setenv(keys_env, "secret-1;secret-2")
    no_key = client.get(probe)
    bad_key = client.get(probe, headers={"X-API-Key": "wrong"})
    good_key = client.get(probe, headers={"X-API-Key": "secret-2"})
    assert no_key.status_code == 401
    assert bad_key.status_code == 401
    assert good_key.status_code == 200

    # 开发模式 + 未配置 → 放行
    monkeypatch.delenv(keys_env, raising=False)
    monkeypatch.setenv("DSH_ENV", "development")
    assert client.get(probe).status_code == 200


def test_strategy_evolution_auth(monkeypatch):
    """strategy-evolution:同矩阵(v1 路由挂依赖,healthz 开放)。"""
    from strategy_evolution.main import app
    client = TestClient(app)
    monkeypatch.delenv("STRATEGY_EVOLUTION_API_KEYS", raising=False)
    monkeypatch.setenv("DSH_ENV", "production")
    assert client.get("/healthz").status_code == 200
    assert client.get("/v1/candidates").status_code == 503
    monkeypatch.setenv("STRATEGY_EVOLUTION_API_KEYS", "evo-key")
    assert client.get("/v1/candidates").status_code == 401
    assert client.get(
        "/v1/candidates", headers={"X-API-Key": "evo-key"}
    ).status_code == 200


def test_gateway_forwards_key_to_risk_policy(monkeypatch):
    """网关调用 risk-policy 时携带 RISK_POLICY_API_KEY。"""
    import httpx
    from quant_gateway import approval_store

    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        resp = httpx.Response(200, json={"passed": True, "limits_hit": []})
        resp.request = httpx.Request("POST", url)
        return resp

    monkeypatch.setattr(approval_store.httpx, "post", fake_post)
    monkeypatch.setenv("RISK_POLICY_API_KEY", "rp-key")
    approval_store.check_order_risk(
        "http://127.0.0.1:8003", market="A_SHARE", account_id="a",
        symbol="s", quantity="1", notional="1", worst_case_loss="0",
        equity="100")
    assert captured["headers"] == {"X-API-Key": "rp-key"}


def test_evolution_forwards_key_to_auditor(monkeypatch):
    """strategy-evolution 调用 risk-auditor 时携带 RISK_AUDITOR_API_KEY。"""
    import httpx
    import strategy_evolution.main as m

    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["headers"] = headers
        resp = httpx.Response(
            200, json={"verdict": "PASS", "conclusion_id": "audit-1"})
        resp.request = httpx.Request("POST", url)
        return resp

    monkeypatch.setattr(m.httpx, "post", fake_post)
    monkeypatch.setenv("RISK_AUDITOR_API_KEY", "ra-key")
    monkeypatch.setenv("STRATEGY_EVOLUTION_AUDITOR_URL", "http://auditor")

    from dsh_contracts import StrategyStage

    class _Req:
        target_stage = StrategyStage.APPROVED
        evidence_refs = ["a:1", "b:2", "c:3"]
        approval_id = "appr-1"

    from dsh_contracts import Market, StrategyCandidate, StrategyStage
    from datetime import UTC, datetime
    candidate = StrategyCandidate(
        candidate_id="c1", market=Market.CRYPTO, strategy_id="s",
        strategy_version="1", stage=StrategyStage.SHADOW,
        updated_at=datetime.now(UTC))
    verdict = m._audit_with_risk_auditor(candidate, _Req(), "hash")
    assert verdict["verdict"] == "PASS"
    assert captured["headers"] == {"X-API-Key": "ra-key"}
