"""跨源同事件归并验收(阶段3遗留 P2)。

同一事件的跨源转载(相同标题)只形成一条决策;不同标题不误并。
"""

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent.parent.parent


def test_extract_same_title_different_sources_share_cluster(tmp_path):
    """两个来源报道同一标题 → 不同 event_id、相同 cluster_key。"""
    from intelligence_ingest.documents import Document
    from intelligence_ingest.extract import extract_event
    from intelligence_ingest.store import IntelligenceStore

    store = IntelligenceStore(str(tmp_path / "intel.db"))
    events = []
    for i, source in enumerate(("src-a", "src-b")):
        doc = Document(
            document_id=f"doc-{i}", source_id=source, source_tier="official",
            canonical_url=f"https://{source}/x", published_at="2026-08-21T09:00:00Z",
            fetched_at="2026-08-21T10:00:00Z", content_hash=f"hash-{i}",
            language="en",
            raw_text="binance officially announces new listing of the token",
            assets=["NEW"], collection_method="RSS",
            title="Binance Announces New Listing", market="CRYPTO")
        store.upsert_document(doc)
        event = extract_event(doc)
        assert event is not None
        store.upsert_event(event)
        events.append(event)
    assert events[0]["event_id"] != events[1]["event_id"]
    assert events[0]["cluster_key"] == events[1]["cluster_key"]

    # 快照导出携带 cluster_key
    snap = store.export_snapshot(tmp_path / "INTELLIGENCE.json")
    exported = [e for e in snap["events"] if e.get("cluster_key")]
    assert len(exported) == 2
    assert len({e["cluster_key"] for e in exported}) == 1


def test_different_titles_do_not_merge():
    from intelligence_ingest.extract import event_cluster_key
    a = event_cluster_key("Binance Lists Token", "LISTING", ["X"])
    b = event_cluster_key("Token Rallies After Listing", "LISTING", ["X"])
    assert a != b


def test_runtime_cluster_dedupe_single_decision():
    """runtime:同 cluster 的两个源 → 只形成一条 item + 一个 Shadow。"""
    from dsh_runtime import BotSession, load_profile, reset
    from dsh_runtime.intelligence import BotIntelligenceJob, SourceSpec

    reset()
    session = BotSession.for_profile(
        load_profile(ROOT / "profiles" / "crypto-bot" / "profile.yaml"))
    job = BotIntelligenceJob(
        bot_name="crypto-bot", market="CRYPTO",
        source_env="DSH_UNUSED", watchlist=("BTCUSDT",),
        default_quantity="0.01")
    base = {
        "title": "交易所公告:合作确认", "symbol": "BTCUSDT",
        "direction": "UP", "action": "BUY", "importance": 0.9,
        "confidence": 0.8, "published_at": "2026-08-21T09:00:00Z",
    }
    spec_a = SourceSpec(source_id="source-a", market="CRYPTO",
                        url="https://a.example", authority="official")
    spec_b = SourceSpec(source_id="source-b", market="CRYPTO",
                        url="https://b.example", authority="official")
    first = job._process_raw_item(
        session=session, spec=spec_a, symbols_held=set(), marks={}, now=None,
        raw={**base, "url": "https://a.example/1", "cluster_key": "clu-same"})
    second = job._process_raw_item(
        session=session, spec=spec_b, symbols_held=set(), marks={}, now=None,
        raw={**base, "url": "https://b.example/1", "cluster_key": "clu-same"})
    # 第二条被 cluster 去重:不插入、不重复 Shadow
    assert first is not None
    assert second is None or second.get("item_id") == first.get("item_id")
    items = session.intelligence.list(limit=10)
    titles = [i.get("title") for i in items if i.get("title") == base["title"]]
    assert len(titles) == 1, "同一 cluster 只允许一条 item"
    assert len(session.tasks.find_by_status("SHADOW_RECORDED")) == 1

    # 无 cluster 的旧路径不受影响:同源同标题仍按逐源键去重,
    # 不同源各成一条(旧行为保留)
    third = job._process_raw_item(
        session=session, spec=spec_a, symbols_held=set(), marks={}, now=None,
        raw={**base, "title": "另一独立事件", "url": "https://a.example/2"})
    assert third is not None


def test_runtime_no_cluster_falls_back_to_source_dedupe():
    from dsh_runtime import BotSession, load_profile, reset
    from dsh_runtime.intelligence import BotIntelligenceJob, SourceSpec

    reset()
    session = BotSession.for_profile(
        load_profile(ROOT / "profiles" / "crypto-bot" / "profile.yaml"))
    job = BotIntelligenceJob(
        bot_name="crypto-bot", market="CRYPTO",
        source_env="DSH_UNUSED", watchlist=("BTCUSDT",),
        default_quantity="0.01")
    spec = SourceSpec(source_id="legacy", market="CRYPTO",
                      url="https://l.example", authority="official")
    raw = {
        "title": "旧格式事件", "symbol": "BTCUSDT", "direction": "UP",
        "action": "BUY", "importance": 0.9, "confidence": 0.8,
        "published_at": "2026-08-21T09:00:00Z",
        "url": "https://l.example/1",
    }
    first = job._process_raw_item(
        session=session, spec=spec, symbols_held=set(), marks={}, now=None, raw=raw)
    again = job._process_raw_item(
        session=session, spec=spec, symbols_held=set(), marks={}, now=None, raw=raw)
    assert first is not None
    assert again is None or again.get("item_id") == first.get("item_id")
