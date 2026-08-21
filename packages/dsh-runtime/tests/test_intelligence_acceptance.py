"""情报到执行闭环验收测试。

对照产品验收标准:
1. 24h 无人值守:主循环单轮异常不退出,退避后继续
2. 源中断可检测并恢复补采(source_health + recovery)
3. 无证据不成决策:缺证据的高重要度事件只 OBSERVE,不形成 Shadow
4. 延迟 SLA 度量:published_at→Shadow 形成延迟入账,日报含 p50/p95/违约数
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent.parent

sys.path.insert(0, str(ROOT / "packages" / "dsh-runtime" / "src"))
sys.path.insert(0, str(ROOT / "packages" / "domain-contracts" / "src"))
sys.path.insert(0, str(ROOT / "services" / "intelligence-ingest" / "src"))


# ---- 1. 主循环守护 ----

def test_loop_survives_cycle_exception():
    """单轮异常:循环记录错误并继续(退避后),第二轮成功恢复正常。"""
    import importlib.util

    script = ROOT / "scripts" / "run_autonomous_layer.py"
    spec = importlib.util.spec_from_file_location("ral", script)
    ral = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ral)

    state = {"cycles": 0, "sleeps": 0}
    logged: list[dict] = []

    def flaky_cycle():
        state["cycles"] += 1
        if state["cycles"] == 1:
            raise RuntimeError("boom: transient fetch failure")
        return {"ok": True}

    def fake_sleep(seconds):
        state["sleeps"] += 1
        if state["sleeps"] >= 2:
            raise ral.StopLoop()

    ral.run_forever(
        flaky_cycle, base_interval=60, sleep=fake_sleep,
        log=logged.append,
    )
    assert state["cycles"] == 2, "第二轮必须在异常后继续执行"
    first, second = logged
    assert first["_loop"]["status"] == "error"
    assert "boom" in first["_loop"]["error"]
    assert first["_loop"]["consecutive_failures"] == 1
    assert first["_loop"]["next_interval_seconds"] == 60  # 线性退避: 60 * 失败数
    assert second["_loop"]["status"] == "ok"
    assert second["_loop"]["consecutive_failures"] == 0
    assert second["ok"] is True


def test_loop_backoff_caps_at_10x():
    import importlib.util

    script = ROOT / "scripts" / "run_autonomous_layer.py"
    spec = importlib.util.spec_from_file_location("ral", script)
    ral = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ral)

    state = {"cycles": 0, "sleeps": 0}
    logged: list[dict] = []

    def always_fails():
        state["cycles"] += 1
        raise ConnectionError("down")

    def fake_sleep(seconds):
        state["sleeps"] += 1
        if state["sleeps"] >= 12:
            raise ral.StopLoop()

    ral.run_forever(always_fails, base_interval=60, sleep=fake_sleep,
                    log=logged.append)
    intervals = [entry["_loop"]["next_interval_seconds"] for entry in logged]
    # 线性退避 60*1, 60*2, ... 直到 60*10 封顶
    assert intervals[:4] == [60, 120, 180, 240]
    assert max(intervals) == 600  # 上限 = base_interval * 10
    assert intervals.count(600) >= 3  # 第 10 次失败后保持封顶


# ---- 2. 源健康与恢复补采 ----

def test_source_health_tracks_failure_and_recovery():
    from intelligence_ingest.store import IntelligenceStore

    store = IntelligenceStore()
    # 第一次失败
    store.record_source_result("src-rss", ok=False, error="conn refused", now="2026-08-21T10:00:00Z")
    h = store.get_source_health("src-rss")
    assert h["consecutive_failures"] == 1
    assert h["last_failure_at"] == "2026-08-21T10:00:00Z"
    assert h["last_success_at"] is None
    # 第二次失败:累计
    store.record_source_result("src-rss", ok=False, error="timeout", now="2026-08-21T10:05:00Z")
    assert store.get_source_health("src-rss")["consecutive_failures"] == 2
    # 恢复:失败清零,记恢复时间,带补采文档数
    recovered = store.record_source_result(
        "src-rss", ok=True, documents=7, now="2026-08-21T10:10:00Z")
    assert recovered["consecutive_failures"] == 0
    assert recovered["last_success_at"] == "2026-08-21T10:10:00Z"
    assert recovered["last_recovery_at"] == "2026-08-21T10:10:00Z"
    assert recovered["last_documents"] == 7
    assert recovered["last_error"] is None
    # 再成功:不再标记恢复
    again = store.record_source_result(
        "src-rss", ok=True, documents=1, now="2026-08-21T10:15:00Z")
    assert again["last_recovery_at"] == "2026-08-21T10:10:00Z"


def test_pipeline_records_health_and_recovery(tmp_path, monkeypatch):
    """pipeline:源失败落健康表;恢复轮标记 recoveries(=补采完成)。"""
    from intelligence_ingest import pipeline
    from intelligence_ingest.documents import Document
    from intelligence_ingest.registry import SourceSpec
    from intelligence_ingest.store import IntelligenceStore

    calls = {"n": 0}

    def flaky_fetch(source, http):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("source down")
        return {"ok": True}

    def collect(source, fetched):
        assert fetched == {"ok": True}
        return [Document(
            document_id="doc-h1", source_id=source.id, source_tier="official",
            canonical_url="https://x.example/h1",
            published_at="2026-08-21T09:00:00Z",
            fetched_at="2026-08-21T10:00:00Z",
            content_hash="hash-h1", language="en",
            raw_text="official announcement says partnership confirmed today",
            assets=["BTC"], collection_method="RSS",
            title="Partnership", market="CRYPTO",
        )]

    monkeypatch.setattr(pipeline, "fetch_source", flaky_fetch)
    monkeypatch.setattr(pipeline, "collect_source", collect)
    src = SourceSpec(id="src-flaky", market="CRYPTO", tier="official",
                     method="RSS", enabled=True, url="https://x.example/rss",
                     min_interval_seconds=0)

    class FakeRegistry:
        def enabled_sources(self, environ=None):
            return [src]
        us_market = []
        crypto_assets = []
        sources = []
        x_filter_rules = []

    store = IntelligenceStore(str(tmp_path / "intel.db"))

    first = pipeline.ingest_once(registry=FakeRegistry(), store=store)
    assert first["errors"] and "src-flaky" in first["errors"][0]
    health = store.get_source_health("src-flaky")
    assert health["consecutive_failures"] == 1

    second = pipeline.ingest_once(registry=FakeRegistry(), store=store)
    assert second["errors"] == []
    assert any("recovered" in r for r in second["recoveries"])
    assert second["documents"] == 1
    # 快照导出包含源健康
    snap = store.export_snapshot(tmp_path / "INTELLIGENCE.json")
    assert any(h["source_id"] == "src-flaky" for h in snap["source_health"])


def test_source_health_endpoint():
    from fastapi.testclient import TestClient
    from intelligence_ingest.main import app
    from intelligence_ingest.store import IntelligenceStore

    client = TestClient(app)
    resp = client.get("/v1/source-health")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


# ---- 3. 无证据不成决策 ----

def test_high_importance_without_evidence_stays_observe():
    from dsh_runtime.intelligence import BotIntelligenceJob
    from dsh_runtime import BotSession, load_profile, reset
    from pathlib import Path as P

    reset()
    profiles = P(ROOT / "profiles")
    session = BotSession.for_profile(load_profile(profiles / "crypto-bot" / "profile.yaml"))
    job = BotIntelligenceJob(
        bot_name="crypto-bot", market="CRYPTO",
        source_env="DSH_TEST_SOURCES_UNUSED", watchlist=("BTCUSDT",),
        default_quantity="0.01",
    )
    item = {
        "source_id": "manual-feed",
        "title": "交易所公告:合作确认",
        "symbol": "BTCUSDT",
        "direction": "UP",
        "action": "BUY",
        "importance": 0.9,
        "confidence": 0.8,
        "published_at": "2026-08-21T09:00:00Z",
        "url": "",  # 无存证 URL → 无证据
    }
    from dsh_runtime.intelligence import SourceSpec as _Spec
    spec = _Spec(source_id="manual-feed", market="CRYPTO",
                 label="Manual", url="", authority="official")
    payload = job._process_raw_item(
        session, spec=spec, raw=item, symbols_held=set(), marks={}, now=None)
    assert payload["no_shadow_reason"] == "missing_evidence"
    assert payload["evidence_refs"] == []
    # 无 Shadow 任务,账本为 OBSERVE
    assert session.tasks.find_by_status("SHADOW_RECORDED") == []
    ledger = session.ledger.find_by_intelligence_item(payload["item_id"])
    assert ledger is not None
    assert ledger["status"] == "OBSERVE"


def test_high_importance_with_evidence_forms_shadow():
    from pathlib import Path as P
    from dsh_runtime import BotSession, load_profile, reset
    from dsh_runtime.intelligence import BotIntelligenceJob, SourceSpec

    reset()
    session = BotSession.for_profile(load_profile(P(ROOT / "profiles") / "crypto-bot" / "profile.yaml"))
    job = BotIntelligenceJob(
        bot_name="crypto-bot", market="CRYPTO",
        source_env="DSH_TEST_SOURCES_UNUSED", watchlist=("BTCUSDT",),
        default_quantity="0.01",
    )
    item = {
        "source_id": "official-feed",
        "title": "交易所公告:合作确认",
        "symbol": "BTCUSDT",
        "direction": "UP",
        "action": "BUY",
        "importance": 0.9,
        "confidence": 0.8,
        "published_at": "2026-08-21T09:00:00Z",
        "url": "https://exchange.example/ann/1",
    }
    spec = SourceSpec(source_id="official-feed", market="CRYPTO",
                      label="Official", url="https://exchange.example",
                      authority="official")
    payload = job._process_raw_item(
        session, spec=spec, raw=item, symbols_held=set(), marks={}, now=None)
    assert "no_shadow_reason" not in payload or payload.get("no_shadow_reason") is None
    assert payload["evidence_refs"] == ["https://exchange.example/ann/1"]
    assert len(session.tasks.find_by_status("SHADOW_RECORDED")) == 1
    # 延迟已度量
    assert payload["latency_seconds"] is not None


# ---- 4. 延迟 SLA 度量 ----

def test_latency_computed_and_reported():
    from datetime import UTC, datetime
    from pathlib import Path as P
    from dsh_runtime import BotSession, load_profile, reset
    from dsh_runtime.intelligence import (
        SHADOW_LATENCY_SLA_SECONDS,
        BotIntelligenceJob,
        SourceSpec,
        StrategyAuditorJob,
    )

    reset()
    session = BotSession.for_profile(load_profile(P(ROOT / "profiles") / "crypto-bot" / "profile.yaml"))
    job = BotIntelligenceJob(
        bot_name="crypto-bot", market="CRYPTO",
        source_env="DSH_TEST_SOURCES_UNUSED", watchlist=("BTCUSDT",),
        default_quantity="0.01",
    )
    spec = SourceSpec(source_id="s", market="CRYPTO",
                      label="S", url="https://s.example", authority="official")
    observed = datetime(2026, 8, 21, 10, 0, tzinfo=UTC)
    # 事件 09:50 发布,10:00 观测 → 延迟 600s(在 SLA 内)
    job._process_raw_item(session, spec=spec, raw={
        "source_id": "s", "title": "在 SLA 内的事件", "symbol": "BTCUSDT",
        "direction": "UP", "action": "BUY", "importance": 0.9, "confidence": 0.8,
        "published_at": "2026-08-21T09:50:00Z", "url": "https://s.example/a",
    }, symbols_held=set(), marks={}, now=observed)
    # 事件 08:00 发布,10:00 观测 → 延迟 7200s(SLA 违约)
    job._process_raw_item(session, spec=spec, raw={
        "source_id": "s", "title": "迟到的事件", "symbol": "BTCUSDT",
        "direction": "UP", "action": "BUY", "importance": 0.9, "confidence": 0.8,
        "published_at": "2026-08-21T08:00:00Z", "url": "https://s.example/b",
    }, symbols_held=set(), marks={}, now=observed)

    auditor = StrategyAuditorJob(
        bot_name="crypto-bot", market="CRYPTO", report_kind="intelligence-daily")
    report = auditor.run(session, now=observed)
    latency = report["latency"]
    assert latency["samples"] == 2
    assert latency["p50_seconds"] == 600.0
    assert latency["max_seconds"] == 7200.0
    assert latency["sla_seconds"] == SHADOW_LATENCY_SLA_SECONDS
    assert latency["sla_violations"] == 1
