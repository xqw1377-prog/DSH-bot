"""采集 → 存证 → 抽取 → 评分。事件只进 Shadow。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from intelligence_ingest.collectors import collect_source, fetch_source
from intelligence_ingest.documents import utc_now
from intelligence_ingest.extract import extract_event
from intelligence_ingest.fetch import Fetcher
from intelligence_ingest.impact import score_event
from intelligence_ingest.isolation import IsolationError, assert_isolated
from intelligence_ingest.registry import SourceRegistry, expand_derived_sources, load_registry
from intelligence_ingest.store import IntelligenceStore


def held_assets_from_snapshots(snapshot_dir: str | Path | None) -> list[str]:
    root = Path(snapshot_dir or os.environ.get("QUANT_GATEWAY_SNAPSHOT_DIR") or "")
    symbols: list[str] = []
    if not root.is_dir():
        return symbols
    for name in ("CRYPTO.json", "A_SHARE.json"):
        path = root / name
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for row in payload.get("positions") or []:
            symbol = str((row or {}).get("symbol") or "")
            if symbol:
                symbols.append(symbol)
    return symbols


def ingest_once(
    *,
    registry: SourceRegistry | None = None,
    store: IntelligenceStore | None = None,
    fetcher: Fetcher | None = None,
    include_derived: bool = False,
    held_assets: list[str] | None = None,
    snapshot_dir: str | Path | None = None,
    environ: dict[str, str] | None = None,
) -> dict[str, Any]:
    assert_isolated(environ)
    loaded = registry or load_registry()
    db = store or IntelligenceStore()
    http = fetcher or Fetcher()
    sources = list(loaded.enabled_sources(environ))
    if include_derived:
        sources.extend(expand_derived_sources(loaded))
    held = held_assets if held_assets is not None else held_assets_from_snapshots(snapshot_dir)
    documents = 0
    events = 0
    errors: list[str] = []
    skipped: list[str] = []
    recoveries: list[str] = []
    for source in sources:
        if source.method in {"PLAYWRIGHT", "SELENIUM", "X_BROWSER"}:
            raise IsolationError(f"refusing forbidden collector {source.method}")
        if source.method == "X_FILTERED_STREAM":
            skipped.append(f"{source.id}: waiting for X_BEARER_TOKEN")
            continue
        # 速率约束:距上次尝试不足 min_interval_seconds 的源本轮跳过
        interval = getattr(source, "min_interval_seconds", 0) or 0
        if interval > 0:
            health = db.get_source_health(source.id)
            last_attempt = (health or {}).get("last_attempt_at")
            if last_attempt:
                try:
                    from datetime import datetime
                    from intelligence_ingest.documents import utc_now as _now
                    prev = datetime.fromisoformat(str(last_attempt).replace("Z", "+00:00"))
                    cur = datetime.fromisoformat(_now().replace("Z", "+00:00"))
                    if (cur - prev).total_seconds() < interval:
                        skipped.append(
                            f"{source.id}: rate-limited (min_interval={interval}s)"
                        )
                        continue
                except ValueError:
                    pass  # 时间不可解析则不拦,交给失败关闭的错误路径
        source_docs = 0
        try:
            fetched = fetch_source(source, http)
            for doc in collect_source(source, fetched):
                db.upsert_document(doc)
                documents += 1
                source_docs += 1
                extracted = extract_event(doc)
                if extracted is None:
                    continue
                scored = score_event(extracted, held_assets=held)
                if scored["mode"] != "SHADOW" or scored["can_apply"]:
                    raise IsolationError("intelligence events must stay SHADOW and not apply")
                db.upsert_event(scored)
                events += 1
        except IsolationError:
            raise
        except Exception as exc:
            errors.append(f"{source.id}: {exc}")
            db.record_source_result(source.id, ok=False, error=str(exc))
            continue
        after = db.record_source_result(
            source.id, ok=True, documents=source_docs
        )
        if after and after.get("last_recovery_at") == after.get("last_success_at"):
            # 恢复即补采:RSS/Atom 拉取最近条目,成功的一拉已覆盖中断窗口
            recoveries.append(
                f"{source.id}: recovered after "
                f"{after.get('consecutive_failures', 0)} failure(s); catch-up fetch done"
            )
    # 24h 采集增长控制:顺路触发 TTL 清理(时间戳守卫,至多每天一次)
    from intelligence_ingest.store import maybe_prune
    maybe_prune(db)
    dest = _snapshot_path(snapshot_dir)
    snapshot = db.export_snapshot(dest) if dest else None
    return {
        "as_of": utc_now(),
        "documents": documents,
        "events": events,
        "errors": errors,
        "skipped": skipped,
        "recoveries": recoveries,
        "source_health": db.list_source_health(),
        "mode": "SHADOW",
        "snapshot": str(dest) if dest else None,
        "held_assets": held,
        "coverage": {
            "x_stream": False,
            "us_quotes": False,
            "cninfo": False,
            "playwright": False,
        },
        "events_preview": (snapshot or {}).get("events", db.recent_events(10)),
    }


def _snapshot_path(snapshot_dir: str | Path | None) -> Path | None:
    root = snapshot_dir or os.environ.get("QUANT_GATEWAY_SNAPSHOT_DIR")
    if not root:
        return None
    return Path(root) / "INTELLIGENCE.json"
