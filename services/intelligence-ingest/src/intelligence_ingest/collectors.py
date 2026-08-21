"""按来源类型采集。没有万能爬虫，也没有 Playwright。"""

from __future__ import annotations

from html.parser import HTMLParser
from xml.etree import ElementTree as ET
from urllib.parse import urljoin, urlparse

try:
    import feedparser
except ModuleNotFoundError:  # pragma: no cover - fallback covered through behavior tests
    feedparser = None

from intelligence_ingest.documents import Document, make_document
from intelligence_ingest.fetch import Fetcher
from intelligence_ingest.registry import SourceSpec


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        href = dict(attrs).get("href")
        if href:
            self._href = href
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href is not None:
            title = " ".join(part.strip() for part in self._text).strip()
            if title:
                self.links.append((self._href, title))
            self._href = None
            self._text = []


def collect_feed(source: SourceSpec, fetched: FetchedText, *, limit: int = 20) -> list[Document]:
    parsed = (
        feedparser.parse(fetched.text)
        if feedparser is not None
        else _fallback_feed_parse(fetched.text)
    )
    docs: list[Document] = []
    for entry in parsed.entries[:limit]:
        title = str(entry.get("title") or "")
        published = _entry_time(entry)
        summary = str(entry.get("summary") or entry.get("description") or title)
        url = str(entry.get("link") or "")
        if source.method == "NASDAQ_HALT_RSS":
            symbol = str(entry.get("ndaq_issuesymbol") or title)
            reason = str(entry.get("ndaq_reasoncode") or "")
            name = str(entry.get("ndaq_issuename") or "")
            if not url:
                url = f"https://www.nasdaqtrader.com/Trader.aspx?id=TradeHalts#{symbol}:{published}"
            title = f"NASDAQ trade halt {symbol} {name}".strip()
            summary = f"Trade halt {symbol} {name} reason={reason}. {summary}"
        raw = f"{title}\n{summary}".strip()
        if not url or not published:
            continue
        docs.append(
            make_document(
                source_id=source.id,
                source_tier=source.tier,
                canonical_url=url,
                published_at=published,
                raw_text=raw,
                assets=list(source.assets),
                collection_method=source.method,
                title=title,
                language=_guess_lang(raw),
                market=source.market,
            )
        )
    return docs


def _fallback_feed_parse(text: str):
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return type("ParsedFeed", (), {"entries": []})()
    entries: list[dict] = []
    atom_ns = "{http://www.w3.org/2005/Atom}"
    if root.tag == f"{atom_ns}feed":
        for entry in root.findall(f"{atom_ns}entry"):
            entries.append(
                {
                    "title": _xml_text(entry.find(f"{atom_ns}title")),
                    "summary": _xml_text(entry.find(f"{atom_ns}summary")),
                    "description": _xml_text(entry.find(f"{atom_ns}content")),
                    "updated": _xml_text(entry.find(f"{atom_ns}updated")),
                    "published": _xml_text(entry.find(f"{atom_ns}published")),
                    "link": (entry.find(f"{atom_ns}link").attrib.get("href") if entry.find(f"{atom_ns}link") is not None else ""),
                }
            )
    else:
        channel = root.find("channel")
        items = channel.findall("item") if channel is not None else []
        for item in items:
            entries.append(
                {
                    "title": _xml_text(item.find("title")),
                    "summary": _xml_text(item.find("description")),
                    "description": _xml_text(item.find("description")),
                    "published": _xml_text(item.find("pubDate")),
                    "link": _xml_text(item.find("link")),
                    "ndaq_issuesymbol": _xml_text(item.find("ndaq_issuesymbol")),
                    "ndaq_reasoncode": _xml_text(item.find("ndaq_reasoncode")),
                    "ndaq_issuename": _xml_text(item.find("ndaq_issuename")),
                }
            )
    return type("ParsedFeed", (), {"entries": entries})()


def _xml_text(node) -> str:
    if node is None or node.text is None:
        return ""
    return node.text.strip()


def collect_html_listing(source: SourceSpec, fetched: FetchedText, *, limit: int = 20) -> list[Document]:
    parser = _LinkParser()
    parser.feed(fetched.text)
    docs: list[Document] = []
    seen: set[str] = set()
    for href, title in parser.links:
        url = urljoin(source.url, href)
        path = urlparse(url).path or "/"
        if source.allow_path_prefixes and not any(path.startswith(prefix) for prefix in source.allow_path_prefixes):
            continue
        if url in seen or url == source.url:
            continue
        seen.add(url)
        # 列表页没有正文：只存发现记录，不能进影响评分（eligible_for_impact=False）。
        docs.append(
            make_document(
                source_id=source.id,
                source_tier=source.tier,
                canonical_url=url,
                published_at="",
                raw_text=title,
                assets=list(source.assets),
                collection_method=source.method,
                title=title,
                language=_guess_lang(title),
                market=source.market,
            )
        )
        if len(docs) >= limit:
            break
    return docs


class FetchedText:
    def __init__(self, text: str, url: str = ""):
        self.text = text
        self.url = url


def fetch_source(source: SourceSpec, fetcher: Fetcher) -> FetchedText:
    page = fetcher.get(source.url)
    if page.status_code >= 400:
        raise RuntimeError(f"{source.id} fetch failed: {page.status_code}")
    return FetchedText(page.text, page.url)


def collect_source(source: SourceSpec, fetched: FetchedText) -> list[Document]:
    if source.method in {"RSS", "SEC_EDGAR", "NASDAQ_HALT_RSS"}:
        return collect_feed(source, fetched)
    if source.method == "HTML_INCREMENTAL":
        return collect_html_listing(source, fetched)
    if source.method == "X_FILTERED_STREAM":
        raise RuntimeError("X Filtered Stream is not collected without X_BEARER_TOKEN")
    raise RuntimeError(f"unsupported method {source.method}")


def _entry_time(entry: dict) -> str:
    for key in ("published", "updated", "created"):
        value = entry.get(key)
        if value:
            return str(value)
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed:
        return f"{parsed.tm_year:04d}-{parsed.tm_mon:02d}-{parsed.tm_mday:02d}"
    return ""


def _guess_lang(text: str) -> str:
    return "zh" if any("\u4e00" <= char <= "\u9fff" for char in text) else "en"
