import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from dsh_runtime import BotIntelligenceJob, BotSession, StrategyAuditorJob, load_profile, reset
from dsh_runtime.autonomous import run_autonomous_cycle

PROFILES = Path(__file__).resolve().parent.parent.parent.parent / "profiles"


def test_intelligence_job_records_shadow_and_audit(tmp_path, monkeypatch):
    reset()
    monkeypatch.delenv("QUANT_GATEWAY_SNAPSHOT_DIR", raising=False)
    feed = tmp_path / "crypto-feed.json"
    feed.write_text(
        json.dumps(
            [
                {
                    "title": "Ethereum 基金会发布路线调整",
                    "url": "https://example.com/eth-roadmap",
                    "published_at": "2026-08-20T00:00:00+00:00",
                    "symbol": "ETHUSDT",
                    "summary": "官方披露路线调整，短期承压。",
                    "direction": "NEGATIVE",
                    "confidence": 0.72,
                    "price_at_event": "4200",
                    "follow_up_prices": {"1h": "4150", "1d": "4050"},
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(
        "DSH_CRYPTO_INTELLIGENCE_SOURCES",
        json.dumps(
            [
                {
                    "source_id": "eth-foundation",
                    "market": "CRYPTO",
                    "authority": "official",
                    "file": str(feed),
                }
            ]
        ),
    )
    session = BotSession.for_profile(load_profile(PROFILES / "crypto-bot" / "profile.yaml"))
    job = BotIntelligenceJob(
        bot_name="crypto-bot",
        market="CRYPTO",
        source_env="DSH_CRYPTO_INTELLIGENCE_SOURCES",
        watchlist=("ETHUSDT",),
    )
    created = job.run(session, holdings=[{"symbol": "ETHUSDT"}])
    assert len(created) == 1
    intel = session.intelligence.list(limit=10)
    assert len(intel) == 1
    assert intel[0]["action"] == "SELL"
    tasks = session.tasks.find_by_status("SHADOW_RECORDED")
    assert len(tasks) == 1
    assert tasks[0]["kind"] == "intelligence-shadow"
    assert tasks[0]["payload"]["shadow_decision"]["action"] == "SELL"
    assert session.events.query("intelligence/ingested")
    assert session.events.query("intelligence/impact.assessed")
    assert session.events.query("intelligence/shadow.recorded")

    auditor = StrategyAuditorJob(
        bot_name="crypto-bot",
        market="CRYPTO",
        report_kind="intelligence-daily",
    )
    report = auditor.run(session)
    assert report["counts"]["intelligence_items"] == 1
    assert report["score"]["intelligence_hit_rate"] == 1.0
    reports = session.reports.list(report_kind="intelligence-daily")
    assert len(reports) == 1
    assert session.events.query("audit/report.generated")
    reset()


def test_official_snapshot_creates_shadow_and_follow_up(tmp_path, monkeypatch):
    reset()
    (tmp_path / "INTELLIGENCE.json").write_text(
        json.dumps(
            {
                "events": [
                    {
                        "event_id": "evt-eth",
                        "title": "ETH 核心成员发布路线调整",
                        "canonical_url": "https://blog.ethereum.org/roadmap",
                        "published_at": "2026-08-20T00:00:00+00:00",
                        "source_id": "rss-eth",
                        "source_tier": "PRIMARY",
                        "market": "CRYPTO",
                        "affected_assets": ["ETHUSDT"],
                        "direction": "BEARISH",
                        "confidence": "0.72",
                        "event_type": "GOVERNANCE",
                        "document_id": "doc-eth",
                    },
                    {
                        "event_id": "evt-policy",
                        "title": "央行开展公开市场操作",
                        "canonical_url": "https://www.pbc.gov.cn/a",
                        "published_at": "2026-08-20T00:00:00+00:00",
                        "source_id": "pbc-news",
                        "source_tier": "PRIMARY",
                        "market": "A_SHARE",
                        "affected_assets": [],
                        "direction": "UNCERTAIN",
                        "confidence": "0.55",
                        "event_type": "MONETARY_POLICY",
                        "document_id": "doc-pbc",
                    },
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("QUANT_GATEWAY_SNAPSHOT_DIR", str(tmp_path))
    monkeypatch.delenv("DSH_CRYPTO_INTELLIGENCE_SOURCES", raising=False)
    monkeypatch.delenv("DSH_A_SHARE_INTELLIGENCE_SOURCES", raising=False)
    start = datetime(2026, 8, 20, 0, 0, tzinfo=UTC)
    crypto = BotSession.for_profile(load_profile(PROFILES / "crypto-bot" / "profile.yaml"))
    job = BotIntelligenceJob(
        bot_name="crypto-bot",
        market="CRYPTO",
        source_env="DSH_CRYPTO_INTELLIGENCE_SOURCES",
        watchlist=("ETHUSDT",),
    )
    created = job.run(
        crypto,
        holdings=[{"symbol": "ETHUSDT"}],
        marks={"ETHUSDT": "4200"},
        now=start,
    )
    assert len(created) == 1
    assert created[0]["price_at_event"] == "4200"
    assert created[0]["action"] == "SELL"
    tasks = crypto.tasks.find_by_status("SHADOW_RECORDED")
    assert len(tasks) == 1
    assert "仅作 Shadow" in tasks[0]["payload"]["shadow_decision"]["why"]
    job.run(
        crypto,
        holdings=[{"symbol": "ETHUSDT"}],
        marks={"ETHUSDT": "4050"},
        now=start + timedelta(hours=2),
    )
    tracked = crypto.intelligence.list(limit=5)[0]
    hour = next(row for row in tracked["payload"]["follow_up"] if row["checkpoint"] == "1h")
    assert hour["status"] == "DONE"
    assert hour["price"] == "4050"
    assert hour["verdict"] == "CORRECT"
    judged = crypto.ledger.find_by_intelligence_item(tracked["item_id"])
    assert judged["payload"]["judgment"]["correct"] >= 1
    assert judged["payload"]["judgment"]["can_apply"] is False

    ashare = BotSession.for_profile(load_profile(PROFILES / "a-stock-bot" / "profile.yaml"))
    policy = BotIntelligenceJob(
        bot_name="a-stock-bot",
        market="A_SHARE",
        source_env="DSH_A_SHARE_INTELLIGENCE_SOURCES",
    ).run(ashare, holdings=[], marks={}, now=start)
    assert len(policy) == 1
    assert policy[0]["action"] == "WATCH"

    result = run_autonomous_cycle(profiles_root=PROFILES, ingest=None, snapshot_dir=tmp_path, now=start)
    assert result["mode"] == "SHADOW"
    assert result["can_apply"] is False
    assert result["trade_blocked"] is True
    reset()


def test_us_spillover_enters_ashare_bot_as_observe(tmp_path, monkeypatch):
    """第二刀：美股是 A 股 Bot 的输入面，但与持仓无关的事件只观察不决策。"""
    reset()
    (tmp_path / "INTELLIGENCE.json").write_text(
        json.dumps(
            {
                "events": [
                    {
                        "event_id": "evt-us-halt",
                        "title": "NASDAQ trade halt GRNQ Greenpro Capital Corp.",
                        "canonical_url": "https://www.nasdaqtrader.com/halt",
                        "published_at": "2026-08-20T04:00:00+00:00",
                        "source_id": "sec-halts",
                        "source_tier": "PRIMARY",
                        "market": "US",
                        "affected_assets": [],
                        "direction": "UNCERTAIN",
                        "confidence": "0.40",
                        "event_type": "TRADE_HALT",
                        "document_id": "doc-halt",
                        "cluster_key": "clu-nasdaq-grnq",
                    },
                    {
                        # 同一事件的转载：同 cluster_key 不同源，只形成一条决策
                        "event_id": "evt-us-halt-repost",
                        "title": "NASDAQ trade halt GRNQ Greenpro Capital Corp.",
                        "canonical_url": "https://example-news.com/repost-grnq",
                        "published_at": "2026-08-20T04:30:00+00:00",
                        "source_id": "news-repost",
                        "source_tier": "SECONDARY",
                        "market": "US",
                        "affected_assets": [],
                        "direction": "NEGATIVE",
                        "confidence": "0.55",
                        "event_type": "TRADE_HALT",
                        "document_id": "doc-halt-repost",
                        "cluster_key": "clu-nasdaq-grnq",
                    },
                    {
                        "event_id": "evt-crypto-x",
                        "title": "某币所上线新交易对",
                        "canonical_url": "https://example.com/listing",
                        "published_at": "2026-08-20T04:00:00+00:00",
                        "source_id": "exchange-news",
                        "source_tier": "SECONDARY",
                        "market": "CRYPTO",
                        "affected_assets": [],
                        "direction": "UNCERTAIN",
                        "confidence": "0.40",
                        "event_type": "LISTING",
                        "document_id": "doc-listing",
                    },
                    {
                        "event_id": "evt-eth-sue",
                        "title": "ETH 项目方被提起诉讼",
                        "canonical_url": "https://example.com/eth-lawsuit",
                        "published_at": "2026-08-20T04:00:00+00:00",
                        "source_id": "sec-enforce",
                        "source_tier": "PRIMARY",
                        "market": "CRYPTO",
                        "affected_assets": ["ETHUSDT"],
                        "direction": "NEGATIVE",
                        "confidence": "0.55",
                        "event_type": "REGULATION",
                        "document_id": "doc-sue",
                    },
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("QUANT_GATEWAY_SNAPSHOT_DIR", str(tmp_path))
    monkeypatch.delenv("DSH_A_SHARE_INTELLIGENCE_SOURCES", raising=False)
    start = datetime(2026, 8, 20, 4, 0, tzinfo=UTC)
    ashare = BotSession.for_profile(load_profile(PROFILES / "a-stock-bot" / "profile.yaml"))
    created = BotIntelligenceJob(
        bot_name="a-stock-bot",
        market="A_SHARE",
        source_env="DSH_A_SHARE_INTELLIGENCE_SOURCES",
    ).run(ashare, holdings=[], marks={}, now=start)
    # 美股事件进入 A 股 Bot；币市场事件不进；
    # 同 cluster_key 的转载不重复决策（双源只入账首条）
    assert len(created) == 1
    assert created[0]["event_market"] == "US"
    # 与持仓无关：只观察，不形成 Shadow 决策
    assert created[0]["action"] == "WATCH"
    assert created[0]["execution_lane"] == "OBSERVE"
    assert ashare.tasks.find_by_status("SHADOW_RECORDED") == []
    # 但观察同样入决策账本：有事件链 ID 与原文证据
    row = ashare.ledger.find_by_intelligence_item(created[0]["item_id"])
    assert row is not None
    assert row["status"] == "OBSERVE"
    assert row["event_id"] == "evt-us-halt"
    assert row["evidence_refs"]
    # 重跑幂等：不重复入账
    again = BotIntelligenceJob(
        bot_name="a-stock-bot",
        market="A_SHARE",
        source_env="DSH_A_SHARE_INTELLIGENCE_SOURCES",
    ).run(ashare, holdings=[], marks={}, now=start + timedelta(minutes=5))
    assert again == []
    assert len(ashare.intelligence.list(limit=10)) == 1
    # 对照：推断方向 NEGATIVE 且命中持仓的事件 → 真实形成 Shadow（退出保护）
    monkeypatch.delenv("DSH_CRYPTO_INTELLIGENCE_SOURCES", raising=False)
    crypto = BotSession.for_profile(load_profile(PROFILES / "crypto-bot" / "profile.yaml"))
    crypto_items = BotIntelligenceJob(
        bot_name="crypto-bot",
        market="CRYPTO",
        source_env="DSH_CRYPTO_INTELLIGENCE_SOURCES",
        watchlist=("ETHUSDT",),
    ).run(crypto, holdings=[{"symbol": "ETHUSDT"}], marks={"ETHUSDT": "4200"}, now=start)
    assert len(crypto_items) == 1
    assert crypto_items[0]["action"] == "SELL"
    shadows = crypto.tasks.find_by_status("SHADOW_RECORDED")
    assert len(shadows) == 1
    assert shadows[0]["payload"]["shadow_decision"]["action"] == "SELL"
    reset()
