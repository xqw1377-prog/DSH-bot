from pathlib import Path

import httpx
import pytest

from intelligence_ingest.collectors import collect_source
from intelligence_ingest.documents import make_document
from intelligence_ingest.extract import extract_event
from intelligence_ingest.fetch import Fetcher
from intelligence_ingest.impact import score_event
from intelligence_ingest.isolation import IsolationError, assert_isolated
from intelligence_ingest.pipeline import ingest_once
from intelligence_ingest.registry import EXPECTED_CRYPTO, load_registry, x_filter_rules
from intelligence_ingest.store import IntelligenceStore

ATOM = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>releases</title>
  <entry>
    <title>v1.2.0 security upgrade</title>
    <link href="https://github.com/ethereum/go-ethereum/releases/tag/v1.2.0"/>
    <updated>2026-08-19T00:00:00Z</updated>
    <summary>Critical security patch and protocol upgrade.</summary>
  </entry>
</feed>
"""

HALT = """<?xml version="1.0"?>
<rss version="2.0">
  <channel>
    <title>Trade Halts</title>
    <item>
      <title>XYZ halt in effect</title>
      <link>https://www.nasdaqtrader.com/halt/xyz</link>
      <pubDate>Tue, 19 Aug 2026 10:00:00 GMT</pubDate>
      <description>Trading halt issued by Nasdaq.</description>
    </item>
  </channel>
</rss>
"""

LISTING = """<html><body>
<a href="/zhengce/content/2026-08/19/content_1.htm">国务院印发某某产业政策的通知</a>
</body></html>"""


def test_registry_matches_24_coins_and_forbids_browser_x():
    registry = load_registry()
    assert [item.symbol for item in registry.crypto_assets] == list(EXPECTED_CRYPTO)
    assert registry.us_market["tier"] == "delayed_iex"
    assert registry.us_market["enabled"] is False
    assert all(source.method != "PLAYWRIGHT" for source in registry.sources)
    btc = registry.asset_by_symbol()["BTCUSDT"]
    assert btc.official_x == ""
    assert btc.github == "bitcoin/bitcoin"
    rules = x_filter_rules(registry)
    assert any("from:ethereum" in rule for rule in rules)
    assert any("from:VitalikButerin" in rule for rule in rules)
    assert all("api.x.com" in source.url for source in registry.sources if source.method == "X_FILTERED_STREAM")


def test_isolation_rejects_write_key_and_exchange_secret():
    with pytest.raises(IsolationError):
        assert_isolated({"BINANCE_API_SECRET": "x"})
    with pytest.raises(IsolationError):
        assert_isolated({"QUANT_GATEWAY_API_KEYS": "k/operator:write"})
    with pytest.raises(IsolationError):
        assert_isolated({"DSH_INTEL_ALLOW_X_BROWSER": "1"})
    assert_isolated({"QUANT_GATEWAY_API_KEYS": "shadow-read/shadow-reader:read"})


def test_incomplete_document_cannot_become_event():
    stub = make_document(
        source_id="gov",
        source_tier="PRIMARY",
        canonical_url="https://www.gov.cn/zhengce/1",
        published_at="2026-08-19",
        raw_text="短标题",
        assets=[],
        collection_method="HTML_INCREMENTAL",
        title="短标题",
        market="A_SHARE",
    )
    assert stub.eligible_for_impact() is False
    assert extract_event(stub) is None


def test_sec_form4_and_nasdaq_halt_extract():
    from intelligence_ingest.collectors import FetchedText
    from intelligence_ingest.registry import SourceSpec

    sec = SourceSpec(
        id="sec-current-filings",
        market="US",
        tier="PRIMARY",
        method="SEC_EDGAR",
        enabled=True,
        url="https://www.sec.gov/cgi-bin/browse-edgar",
    )
    sec_docs = collect_source(
        sec,
        FetchedText(
            """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>4 - Example Corp (0001) (Issuer)</title>
    <link href="https://www.sec.gov/Archives/edgar/data/1/index.htm"/>
    <updated>2026-08-19T00:00:00Z</updated>
    <summary>&lt;b&gt;Filed:&lt;/b&gt; 2026-08-19 Form 4 insider</summary>
  </entry>
</feed>"""
        ),
    )
    assert extract_event(sec_docs[0])["event_type"] == "EARNINGS"

    halt = SourceSpec(
        id="nasdaq-trade-halts",
        market="US",
        tier="PRIMARY",
        method="NASDAQ_HALT_RSS",
        enabled=True,
        url="https://www.nasdaqtrader.com/rss.aspx?feed=tradehalts",
    )
    halt_docs = collect_source(halt, FetchedText(HALT))
    event = extract_event(halt_docs[0])
    assert event["event_type"] == "TRADE_HALT"
    assert "nasdaqtrader.com" in halt_docs[0].canonical_url


def test_feed_extract_and_shadow_score(tmp_path):
    from intelligence_ingest.registry import SourceSpec

    source = SourceSpec(
        id="github-eth",
        market="CRYPTO",
        tier="PRIMARY",
        method="RSS",
        enabled=True,
        url="https://github.com/ethereum/go-ethereum/releases.atom",
        assets=["ETHUSDT"],
        event_types=["GOVERNANCE"],
    )
    docs = collect_source(source, type("F", (), {"text": ATOM, "url": source.url})())
    assert docs[0].eligible_for_impact()
    event = extract_event(docs[0])
    assert event["event_type"] == "GOVERNANCE"
    scored = score_event(event, held_assets=["ETHUSDT"])
    assert scored["mode"] == "SHADOW"
    assert scored["can_apply"] is False
    assert float(scored["impact_score"]) > 0


def test_ingest_once_uses_injected_http(tmp_path, monkeypatch):
    monkeypatch.delenv("BINANCE_API_SECRET", raising=False)
    monkeypatch.delenv("QUANT_GATEWAY_API_KEYS", raising=False)
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "nasdaqtrader.com" in url:
            return httpx.Response(200, text=HALT)
        if "sec.gov" in url:
            return httpx.Response(200, text=ATOM)
        if any(host in url for host in ("gov.cn", "pbc.gov.cn", "csrc.gov.cn", "ndrc.gov.cn")):
            return httpx.Response(200, text=LISTING)
        return httpx.Response(404, text="missing")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    store = IntelligenceStore(str(tmp_path / "intel.db"))
    result = ingest_once(
        store=store,
        fetcher=Fetcher(client),
        include_derived=False,
        held_assets=["ETHUSDT"],
        snapshot_dir=tmp_path,
        environ={},
    )
    assert result["mode"] == "SHADOW"
    assert result["documents"] >= 2
    assert result["events"] >= 1
    assert all(event["can_apply"] is False for event in store.recent_events())
    snap = tmp_path / "INTELLIGENCE.json"
    assert snap.is_file()
    assert "SHADOW" in snap.read_text(encoding="utf-8")
