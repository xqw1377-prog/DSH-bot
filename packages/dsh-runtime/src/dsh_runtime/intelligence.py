from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import httpx

from dsh_runtime.ledger import (
    build_entry_plan,
    build_exit_plan,
    classify_intel,
    lane_allows_shadow,
)
from dsh_runtime.shadow import build_shadow_decision


# 与 6celue DEFAULT_SYMBOLS_ORDER 对齐，不导入 6celue 代码。
SIXCELUE_CRYPTO_UNIVERSE = (
    "HYPEUSDT",
    "ENAUSDT",
    "NEARUSDT",
    "TAOUSDT",
    "LINKUSDT",
    "AAVEUSDT",
    "DOGEUSDT",
    "ADAUSDT",
    "XMRUSDT",
    "SUIUSDT",
    "FETUSDT",
    "ORDIUSDT",
    "SOLUSDT",
    "ETHUSDT",
    "XRPUSDT",
    "LTCUSDT",
    "AVAXUSDT",
    "WLDUSDT",
    "BNBUSDT",
    "OPUSDT",
    "TIAUSDT",
    "RENDERUSDT",
    "BTCUSDT",
    "BCHUSDT",
)
DEFAULT_CRYPTO_WATCHLIST = SIXCELUE_CRYPTO_UNIVERSE
CHECKPOINT_HOURS = {"1h": 1, "1d": 24, "3d": 72}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _dec(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


@dataclass(frozen=True)
class SourceSpec:
    source_id: str
    market: str
    authority: str = "official"
    label: str | None = None
    url: str | None = None
    file: str | None = None
    items_key: str | None = None
    symbols: tuple[str, ...] = ()


def _normalize_source_specs(raw: str | None) -> list[SourceSpec]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    specs: list[SourceSpec] = []
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict) or not item.get("source_id"):
            continue
        specs.append(
            SourceSpec(
                source_id=str(item["source_id"]),
                market=str(item.get("market") or "GLOBAL"),
                authority=str(item.get("authority") or "official"),
                label=item.get("label"),
                url=item.get("url"),
                file=item.get("file"),
                items_key=item.get("items_key"),
                symbols=tuple(str(v) for v in (item.get("symbols") or []) if v),
            )
        )
    return specs


def load_source_specs(env_var: str) -> list[SourceSpec]:
    return _normalize_source_specs(os.environ.get(env_var))


def _parse_iso(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def snapshot_dir(path: str | Path | None = None) -> Path | None:
    raw = path or os.environ.get("QUANT_GATEWAY_SNAPSHOT_DIR") or ""
    root = Path(raw) if raw else None
    return root if root and root.is_dir() else None


def marks_from_snapshots(root: str | Path | None = None) -> dict[str, str]:
    marks: dict[str, str] = {}
    base = snapshot_dir(root)
    if base is None:
        return marks
    for name in ("CRYPTO.json", "A_SHARE.json"):
        path = base / name
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for row in payload.get("signals") or []:
            symbol = str((row or {}).get("symbol") or "")
            price = row.get("entry_price") or row.get("price") or row.get("mark_price")
            if symbol and price not in (None, "") and symbol not in marks:
                marks[symbol] = str(price)
        for row in payload.get("positions") or []:
            symbol = str((row or {}).get("symbol") or "")
            price = row.get("last_price") or row.get("mark_price") or row.get("avg_cost")
            if symbol and price not in (None, "") and symbol not in marks:
                marks[symbol] = str(price)
    return marks


def holdings_from_snapshots(root: str | Path | None = None) -> dict[str, list[dict]]:
    base = snapshot_dir(root)
    out = {"CRYPTO": [], "A_SHARE": []}
    if base is None:
        return out
    for market, name in (("CRYPTO", "CRYPTO.json"), ("A_SHARE", "A_SHARE.json")):
        path = base / name
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        out[market] = [row for row in (payload.get("positions") or []) if row.get("symbol")]
    return out


def official_snapshot_items(
    *,
    market: str,
    watchlist: tuple[str, ...] = (),
    root: str | Path | None = None,
) -> list[tuple[SourceSpec, dict]]:
    base = snapshot_dir(root)
    path = (base / "INTELLIGENCE.json") if base else None
    if path is None or not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    watched = {item.upper() for item in watchlist}
    items: list[tuple[SourceSpec, dict]] = []
    for raw in payload.get("events") or []:
        if not isinstance(raw, dict):
            continue
        event_market = str(raw.get("market") or "")
        assets = [str(item).upper() for item in (raw.get("affected_assets") or []) if item]
        symbol = next((item for item in assets if item in watched), assets[0] if assets else "")
        if market == "CRYPTO":
            if symbol and symbol not in watched and symbol not in SIXCELUE_CRYPTO_UNIVERSE:
                continue
            if not symbol and event_market not in {"CRYPTO", ""}:
                continue
        elif market == "A_SHARE":
            # A 股 Bot 的输入面 = 国内政策/美股/公司公告：
            # 美股市场级事件作为外溢观察放行，不在此处判重要性
            if event_market not in {"A_SHARE", "US", "GLOBAL"}:
                continue
        spec = SourceSpec(
            source_id=str(raw.get("source_id") or "official-ingest"),
            market=event_market or market,
            authority="official" if raw.get("source_tier") == "PRIMARY" else "secondary",
            label=str(raw.get("source_id") or "official-ingest"),
            url=str(raw.get("canonical_url") or ""),
            symbols=tuple(assets),
        )
        direction = str(raw.get("direction") or "UNCERTAIN").upper()
        mapped = {
            "title": raw.get("title") or raw.get("event_type"),
            "summary": raw.get("title") or "",
            "url": raw.get("canonical_url"),
            "published_at": raw.get("published_at"),
            "symbol": symbol,
            "direction": (
                "POSITIVE" if direction in {"BULLISH", "POSITIVE"}
                else "NEGATIVE" if direction in {"BEARISH", "NEGATIVE"}
                else "NEUTRAL"
            ),
            "confidence": raw.get("confidence"),
            "evidence_refs": raw.get("evidence_refs") or [raw.get("document_id")],
            "event_type": raw.get("event_type"),
            "event_id": raw.get("event_id"),
            "event_market": event_market,
            "cluster_key": raw.get("cluster_key") or "",
            "tags": [raw.get("event_type")] if raw.get("event_type") else [],
        }
        items.append((spec, mapped))
    return items


def _read_source_payload(spec: SourceSpec) -> list[dict]:
    payload: Any
    if spec.file:
        try:
            payload = json.loads(Path(spec.file).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
    elif spec.url:
        try:
            resp = httpx.get(spec.url, timeout=5.0)
        except httpx.HTTPError:
            return []
        if not resp.is_success:
            return []
        try:
            payload = resp.json()
        except ValueError:
            return []
    else:
        return []
    if spec.items_key and isinstance(payload, dict):
        payload = payload.get(spec.items_key, [])
    if isinstance(payload, dict):
        payload = payload.get("items", payload.get("documents", []))
    return payload if isinstance(payload, list) else []


def _text_bits(*values: Any) -> str:
    return " ".join(str(v) for v in values if v not in (None, "")).lower()


def _direction(raw: dict) -> str:
    direct = str(raw.get("direction") or "").upper()
    if direct in {"POSITIVE", "NEGATIVE", "NEUTRAL"}:
        return direct
    text = _text_bits(raw.get("title"), raw.get("summary"), raw.get("body"))
    if any(word in text for word in ("hack", "attack", "暂停", "suspend", "漏洞", "lawsuit", "down")):
        return "NEGATIVE"
    if any(word in text for word in ("partnership", "approval", "launch", "上线", "获批", "adopt")):
        return "POSITIVE"
    return "NEUTRAL"


def _horizon(raw: dict) -> str:
    direct = str(raw.get("horizon") or "").lower()
    if direct in {"short", "medium", "long"}:
        return direct
    text = _text_bits(raw.get("title"), raw.get("summary"), raw.get("body"))
    if any(word in text for word in ("unlock", "roadmap", "governance", "policy", "listing")):
        return "long"
    if any(word in text for word in ("halt", "暂停", "attack", "hack", "volatility")):
        return "short"
    return "medium"


def _confidence(raw: dict) -> float:
    value = raw.get("confidence")
    try:
        return max(0.0, min(float(value), 1.0))
    except (TypeError, ValueError):
        return 0.5


def _importance(raw: dict, *, held: bool, watched: bool, authority: str) -> float:
    base = 0.45
    if authority in {"official", "regulator", "exchange"}:
        base += 0.2
    if held:
        base += 0.2
    elif watched:
        base += 0.1
    if _direction(raw) != "NEUTRAL":
        base += 0.1
    try:
        extra = float(raw.get("importance") or 0.0)
    except (TypeError, ValueError):
        extra = 0.0
    return max(0.0, min(base + extra, 1.0))


# Shadow 建议延迟 SLA:事件发布 → Shadow 建议形成的目标上限。
# 超过即计入日报 latency.violations(5 分钟轮询下的合理目标)。
SHADOW_LATENCY_SLA_SECONDS = 900.0


def _latency_stats(samples: list[float], *, no_evidence: int = 0) -> dict:
    """Shadow 延迟统计:p50/p95/max + SLA 违约数(验收:重要事件按时形成 Shadow)。"""
    if not samples:
        return {
            "samples": 0, "p50_seconds": None, "p95_seconds": None,
            "max_seconds": None, "sla_seconds": SHADOW_LATENCY_SLA_SECONDS,
            "sla_violations": 0, "no_evidence_items": no_evidence,
        }
    ordered = sorted(samples)

    def pct(fraction: float) -> float:
        import math
        idx = max(0, math.ceil(fraction * len(ordered)) - 1)
        return round(ordered[idx], 1)

    return {
        "samples": len(ordered),
        "p50_seconds": pct(0.50),
        "p95_seconds": pct(0.95),
        "max_seconds": round(ordered[-1], 1),
        "sla_seconds": SHADOW_LATENCY_SLA_SECONDS,
        "sla_violations": sum(
            1 for s in ordered if s > SHADOW_LATENCY_SLA_SECONDS
        ),
        "no_evidence_items": no_evidence,
    }


def _event_latency_seconds(published_at: str | None, observed_at: str | None) -> float | None:
    """事件发布 → 观测(Shadow 形成)的延迟;不可解析返回 None。"""
    if not published_at or not observed_at:
        return None
    try:
        pub = datetime.fromisoformat(str(published_at).replace("Z", "+00:00"))
        obs = datetime.fromisoformat(str(observed_at).replace("Z", "+00:00"))
    except ValueError:
        return None
    if pub.tzinfo is None:
        pub = pub.replace(tzinfo=UTC)
    if obs.tzinfo is None:
        obs = obs.replace(tzinfo=UTC)
    return round((obs - pub).total_seconds(), 1)


def _shadow_why(payload: dict) -> str:
    relation = (
        "与当前持仓直接相关"
        if payload.get("held")
        else "命中 24 币观察池"
        if payload.get("watched")
        else "市场级政策/监管"
    )
    first_hand = "第一手" if payload.get("authority") in {"official", "regulator"} else "二手"
    conf = float(payload.get("confidence") or 0)
    action = str(payload.get("action") or "WATCH")
    if action == "SELL":
        advice = "建议进入退出评估，不加仓；仅 Shadow，不自动减仓。"
    elif action == "HOLD":
        advice = "建议不加仓，继续持有并观察失效条件。"
    elif action == "BUY":
        advice = "观察是否具备进入条件，禁止追涨。"
    else:
        advice = "等待确认，禁止追涨。"
    title = str(payload.get("title") or "事件")
    return (
        f"{title}。来源 {payload.get('source_label') or payload.get('source_id')}，{first_hand}信息；"
        f"{relation}。方向 {payload.get('direction')}，周期 {payload.get('horizon')}，"
        f"置信度 {conf:.0%}。{advice}当前仅作 Shadow 决策。"
    )


def _action(direction: str, *, held: bool, confidence: float) -> str:
    if direction == "NEGATIVE":
        return "SELL" if held and confidence >= 0.55 else "WATCH"
    if direction == "POSITIVE":
        return "HOLD" if held and confidence >= 0.55 else "BUY"
    return "WATCH"


def _follow_up_template(raw: dict) -> list[dict]:
    prices = raw.get("follow_up_prices") or {}
    result = []
    for checkpoint in ("1h", "1d", "3d"):
        result.append(
            {
                "checkpoint": checkpoint,
                "status": "DONE" if checkpoint in prices else "PENDING",
                "price": prices.get(checkpoint),
                "verdict": None,
            }
        )
    return result


def _assess_checkpoints(payload: dict) -> list[dict]:
    event_price = _dec(payload.get("price_at_event"))
    direction = str(payload.get("direction") or "NEUTRAL").upper()
    rows = []
    for row in payload.get("follow_up") or []:
        item = dict(row)
        price = _dec(item.get("price"))
        if item.get("status") == "DONE" and event_price is not None and price is not None:
            if direction == "POSITIVE":
                item["verdict"] = "CORRECT" if price >= event_price else "WRONG"
            elif direction == "NEGATIVE":
                item["verdict"] = "CORRECT" if price <= event_price else "WRONG"
            else:
                item["verdict"] = "NEUTRAL"
        rows.append(item)
    return rows


class BotIntelligenceJob:
    def __init__(
        self,
        *,
        bot_name: str,
        market: str,
        source_env: str,
        watchlist: tuple[str, ...] = (),
        default_quantity: str = "0.01",
    ):
        self.bot_name = bot_name
        self.market = market
        self.source_env = source_env
        self.watchlist = tuple(watchlist)
        self.default_quantity = default_quantity

    def run(
        self,
        session,
        *,
        holdings: list[dict] | None = None,
        marks: dict[str, str] | None = None,
        now: datetime | None = None,
        snapshot_root: str | Path | None = None,
    ) -> list[dict]:
        session.use("intelligence_ingest")
        symbols_held = {
            str(row.get("symbol"))
            for row in (holdings or [])
            if row.get("symbol")
        }
        prices = marks if marks is not None else marks_from_snapshots()
        created: list[dict] = []
        pairs: list[tuple[SourceSpec, dict]] = []
        pairs.extend(
            official_snapshot_items(
                market=self.market,
                watchlist=self.watchlist or SIXCELUE_CRYPTO_UNIVERSE,
                root=snapshot_root,
            )
        )
        for spec in load_source_specs(self.source_env):
            for raw in _read_source_payload(spec):
                pairs.append((spec, raw))
        for spec, raw in pairs:
            item = self._process_raw_item(
                session,
                spec=spec,
                raw=raw,
                symbols_held=symbols_held,
                marks=prices,
                now=now,
            )
            if item is not None:
                created.append(item)
        self.refresh_follow_ups(session, marks=prices, now=now)
        return created

    def refresh_follow_ups(
        self,
        session,
        *,
        marks: dict[str, str],
        now: datetime | None = None,
    ) -> int:
        current = now or datetime.now(UTC)
        updated = 0
        for item in session.intelligence.list(limit=200):
            payload = dict(item.get("payload") or {})
            symbol = str(item.get("symbol") or "")
            changed = False
            if not payload.get("price_at_event") and symbol in marks:
                payload["price_at_event"] = marks[symbol]
                changed = True
            start = _parse_iso(payload.get("observed_at") or item.get("observed_at"))
            follow = [dict(row) for row in (payload.get("follow_up") or [])]
            if start and symbol in marks:
                for row in follow:
                    if row.get("status") == "DONE":
                        continue
                    hours = CHECKPOINT_HOURS.get(str(row.get("checkpoint") or ""), 0)
                    if hours and current >= start + timedelta(hours=hours):
                        row["status"] = "DONE"
                        row["price"] = marks[symbol]
                        changed = True
            if not changed:
                continue
            payload["follow_up"] = _assess_checkpoints({**payload, "follow_up": follow})
            session.intelligence.update_payload(item["item_id"], payload)
            _sync_follow_up_to_ledger(session, item["item_id"], payload)
            updated += 1
        return updated

    def _process_raw_item(
        self,
        session,
        *,
        spec: SourceSpec,
        raw: dict,
        symbols_held: set[str],
        marks: dict[str, str] | None = None,
        now: datetime | None = None,
    ) -> dict | None:
        if not isinstance(raw, dict):
            return None
        session.use("impact_assessment")
        symbol = str(raw.get("symbol") or (raw.get("symbols") or spec.symbols or [""])[0] or "")
        watched = symbol in self.watchlist or (
            self.market == "CRYPTO" and symbol in SIXCELUE_CRYPTO_UNIVERSE
        )
        held = symbol in symbols_held
        event_market = str(raw.get("event_market") or spec.market or "")
        market_wide = (
            not symbol
            and self.market == "A_SHARE"
            and event_market in {"A_SHARE", "US", "GLOBAL"}
        )
        if not watched and not held and not market_wide and self.market != "GLOBAL":
            return None
        title = str(raw.get("title") or raw.get("headline") or "").strip()
        if not title:
            return None
        direction = _direction(raw)
        horizon = _horizon(raw)
        confidence = _confidence(raw)
        importance = _importance(
            raw,
            held=held,
            watched=watched,
            authority=spec.authority,
        )
        action = str(raw.get("action") or _action(direction, held=held, confidence=confidence))
        source_url = str(raw.get("url") or raw.get("source_url") or spec.url or "")
        published_at = str(raw.get("published_at") or raw.get("ts") or "")
        # 跨源归并:同 cluster_key(转载/通稿)只形成一条决策;
        # 无 cluster 的外部源退回逐源去重。
        cluster_key = str(raw.get("cluster_key") or "")
        if cluster_key:
            dedupe_key = hashlib.sha256(
                f"cluster|{cluster_key}|{self.market}".encode()
            ).hexdigest()
        else:
            dedupe_key = hashlib.sha256(
                f"{spec.source_id}|{symbol}|{title}|{source_url}|{published_at}".encode()
            ).hexdigest()
        payload = {
            "title": title,
            "summary": raw.get("summary") or raw.get("body") or "",
            "market": self.market,
            "symbol": symbol or None,
            "source_id": spec.source_id,
            "source_label": spec.label or spec.source_id,
            "source_url": source_url or None,
            "authority": spec.authority,
            "direction": direction,
            "horizon": horizon,
            "confidence": confidence,
            "importance": importance,
            "action": action,
            "held": held,
            "watched": watched,
            "evidence_refs": [source_url] if source_url else [],
            "price_at_event": raw.get("price_at_event") or (marks or {}).get(symbol),
            "follow_up": _follow_up_template(raw),
            "observed_at": now.isoformat() if now else _now_iso(),
            "tags": raw.get("tags") or [],
        }
        payload["follow_up"] = _assess_checkpoints(payload)
        # 延迟 SLA 度量:必须在入库前写入,payload 会整体序列化存档
        payload["latency_seconds"] = _event_latency_seconds(
            published_at, payload["observed_at"]
        )
        payload["latency_sla_seconds"] = SHADOW_LATENCY_SLA_SECONDS
        item_id, inserted = session.intelligence.upsert(
            dedupe_key=dedupe_key,
            market=self.market,
            source_id=spec.source_id,
            symbol=symbol or None,
            title=title,
            source_url=source_url or None,
            published_at=published_at or None,
            observed_at=payload["observed_at"],
            authority=spec.authority,
            direction=direction,
            horizon=horizon,
            importance=importance,
            confidence=confidence,
            action=action,
            payload=payload,
        )
        payload["item_id"] = item_id
        if not inserted:
            # 重复信息不重复决策：已入库事件不再评估、不再入账
            return None
        session.events.emit(
            "intelligence/ingested",
            self.market,
            "bot",
            self.bot_name,
            {
                "item_id": item_id,
                "source_id": spec.source_id,
                "market": self.market,
                "symbol": symbol or None,
                "title": title,
                "source_url": source_url or None,
                "published_at": published_at or payload["observed_at"],
            },
        )
        session.events.emit(
            "intelligence/impact.assessed",
            self.market,
            "bot",
            self.bot_name,
            {
                "item_id": item_id,
                "market": self.market,
                "symbol": symbol or None,
                "direction": direction,
                "horizon": horizon,
                "importance": importance,
                "confidence": confidence,
                "action": action,
                "held": held,
                "watched": watched,
            },
        )
        grade, lane, needs_approval = classify_intel(
            authority=spec.authority,
            source_tier=str(raw.get("source_tier") or spec.authority),
            action=action,
            held=held,
            title=title,
            event_type=str(raw.get("event_type") or ""),
            importance=importance,
        )
        payload["intel_grade"] = grade
        payload["execution_lane"] = lane
        payload["requires_approval"] = needs_approval
        payload["event_id"] = raw.get("event_id")
        payload["event_type"] = raw.get("event_type")
        payload["event_market"] = event_market
        if market_wide and not held and not watched:
            # 市场级事件但与持仓/观察池无关：只观察不决策——
            # 决策是噪音，不记录是失明。OBSERVE 同样入决策账本。
            payload["action"] = "WATCH"
            payload["execution_lane"] = "OBSERVE"
        has_evidence = bool(payload.get("evidence_refs"))
        if not has_evidence:
            # 无证据不成决策:即使 importance 达标也只观察,不形成 Shadow
            payload["no_shadow_reason"] = "missing_evidence"
        if (importance >= 0.55
                and lane_allows_shadow(payload["execution_lane"])
                and has_evidence):
            self._record_shadow(session, payload)
        else:
            self._write_ledger(session, payload, task_id=None, status="OBSERVE")
        return payload

    def _record_shadow(self, session, payload: dict) -> None:
        session.use("shadow_recording")
        subject_id = str(payload["item_id"])
        task_id = session.tasks.create(
            kind="intelligence-shadow",
            subject_id=subject_id,
            payload={
                "market": self.market,
                "symbol": payload.get("symbol"),
                "event_title": payload.get("title"),
                "event_summary": payload.get("summary"),
                "event_source": payload.get("source_label"),
                "event_url": payload.get("source_url"),
                "price_at_event": payload.get("price_at_event"),
                "intelligence_item_id": payload["item_id"],
                "latency_seconds": payload.get("latency_seconds"),
                "latency_sla_seconds": payload.get("latency_sla_seconds"),
                "intelligence_follow_up": payload.get("follow_up"),
                "shadow_decision": build_shadow_decision(
                    {
                        "symbol": payload.get("symbol"),
                        "valid_until": payload.get("published_at") or payload.get("observed_at"),
                        "evidence_refs": payload.get("evidence_refs") or [],
                    },
                    action=str(payload.get("action") or "WATCH"),
                    quantity=self.default_quantity if payload.get("action") in {"BUY", "SELL"} else "0",
                    suggested_price=str(payload.get("price_at_event") or ""),
                    strategy_version="intelligence-v1",
                    strength=float(payload.get("confidence") or 0.0),
                    position_after="0",
                    worst_case_loss="0",
                    primary_risks=[str(tag) for tag in (payload.get("tags") or [])[:3]],
                    why=_shadow_why(payload),
                    why_not="仅作 Shadow 建议，不审批、不下单，不能改线上策略。",
                    skip_reason=None if payload.get("action") in {"BUY", "SELL", "HOLD"} else "WAITING_CONFIRM",
                    evidence_refs=payload.get("evidence_refs") or [],
                    valid_until=payload.get("published_at") or payload.get("observed_at"),
                ),
            },
        )
        task = session.tasks.get(task_id)
        if task is not None and task["status"] != "SHADOW_RECORDED":
            session.tasks.transition(task_id, "SHADOW_RECORDED")
            session.events.emit(
                "intelligence/shadow.recorded",
                self.market,
                "bot",
                self.bot_name,
                {
                    "item_id": payload["item_id"],
                    "task_id": task_id,
                    "market": self.market,
                    "symbol": payload.get("symbol"),
                    "action": payload.get("action"),
                    "confidence": payload.get("confidence"),
                    "latency_seconds": payload.get("latency_seconds"),
                    "latency_sla_seconds": payload.get("latency_sla_seconds"),
                    "sla_breached": (
                        payload.get("latency_seconds") is not None
                        and payload["latency_seconds"] > SHADOW_LATENCY_SLA_SECONDS
                    ),
                },
            )
        self._write_ledger(session, payload, task_id=task_id, status="SHADOW")

    def _write_ledger(self, session, payload: dict, *, task_id: str | None, status: str) -> None:
        session.ledger.upsert(
            {
                "market": self.market,
                "symbol": payload.get("symbol"),
                "status": status,
                "intel_grade": payload.get("intel_grade") or "OBSERVE",
                "execution_lane": payload.get("execution_lane") or "OBSERVE",
                "event_id": payload.get("event_id") or payload.get("item_id"),
                "intelligence_item_id": payload.get("item_id"),
                "signal_id": None,
                "strategy_id": "intelligence-v1",
                "strategy_version": "intelligence-v1",
                "risk_snapshot_id": f"rs-intel-{payload.get('item_id')}",
                "task_id": task_id,
                "capital_budget": "0",
                "max_risk": "0",
                "requires_approval": payload.get("requires_approval", True),
                "action": payload.get("action"),
                "direction": payload.get("direction"),
                "confidence": payload.get("confidence"),
                "impact_horizon": payload.get("horizon"),
                "entry_plan": build_entry_plan(
                    action=str(payload.get("action") or "WATCH"),
                    price=payload.get("price_at_event"),
                    horizon=payload.get("horizon"),
                    max_capital_ratio="0.03" if payload.get("held") else "0.00",
                ),
                "exit_plan": build_exit_plan(
                    action=str(payload.get("action") or "WATCH"),
                    horizon=payload.get("horizon"),
                    event_type=str(payload.get("event_type") or ""),
                ),
                "evidence_refs": payload.get("evidence_refs") or [],
                "payload": {
                    "can_apply": False,
                    "live_blocked": True,
                    "latency_seconds": payload.get("latency_seconds"),
                    "latency_sla_seconds": payload.get("latency_sla_seconds"),
                    "no_shadow_reason": payload.get("no_shadow_reason"),
                },
            }
        )


def _sync_follow_up_to_ledger(session, item_id: str, payload: dict) -> None:
    row = session.ledger.find_by_intelligence_item(item_id)
    if row is None:
        return
    follow = payload.get("follow_up") or []
    done = [item for item in follow if item.get("status") == "DONE"]
    if not done:
        return
    wrong = [item for item in done if item.get("verdict") == "WRONG"]
    correct = [item for item in done if item.get("verdict") == "CORRECT"]
    session.ledger.attach_judgment(
        row["decision_id"],
        {
            "follow_up": follow,
            "correct": len(correct),
            "wrong": len(wrong),
            "invalidation": [
                f"{item.get('checkpoint')} 复盘证明方向错误，建议作废"
                for item in wrong
            ],
        },
    )


class StrategyAuditorJob:
    def __init__(self, *, bot_name: str, market: str, report_kind: str):
        self.bot_name = bot_name
        self.market = market
        self.report_kind = report_kind

    def run(self, session, *, now: datetime | None = None) -> dict:
        session.use("strategy_audit")
        current = now or datetime.now(UTC)
        period_key = current.date().isoformat()
        items = session.intelligence.list(limit=500)
        tasks = session.tasks.find_by_status("SHADOW_RECORDED")
        ledger_coverage = session.ledger.coverage()
        source_stats: dict[str, dict] = {}
        follow_done = 0
        follow_correct = 0
        latency_samples: list[float] = []
        no_evidence = 0
        for item in items:
            payload = item.get("payload") or {}
            source = str(item.get("source_id") or "unknown")
            stat = source_stats.setdefault(source, {"count": 0, "correct": 0, "done": 0})
            stat["count"] += 1
            if payload.get("no_shadow_reason") == "missing_evidence":
                no_evidence += 1
            sample = payload.get("latency_seconds")
            if isinstance(sample, (int, float)) and sample >= 0:
                latency_samples.append(float(sample))
            for checkpoint in payload.get("follow_up") or []:
                if checkpoint.get("verdict") in {"CORRECT", "WRONG"}:
                    stat["done"] += 1
                    follow_done += 1
                    if checkpoint.get("verdict") == "CORRECT":
                        stat["correct"] += 1
                        follow_correct += 1
        top_sources = sorted(
            (
                {
                    "source_id": key,
                    "count": value["count"],
                    "evaluated": value["done"],
                    "hit_rate": (
                        round(value["correct"] / value["done"], 3)
                        if value["done"]
                        else None
                    ),
                }
                for key, value in source_stats.items()
            ),
            key=lambda row: (row["hit_rate"] or 0.0, row["count"]),
            reverse=True,
        )[:5]
        report = {
            "as_of": current.isoformat(),
            "bot": self.bot_name,
            "market": self.market,
            "report_kind": self.report_kind,
            "counts": {
                "intelligence_items": len(items),
                "shadow_decisions": len(tasks),
                "follow_up_done": follow_done,
                "ledger_decisions": ledger_coverage["decisions"],
                "ledger_linked": ledger_coverage["fully_linked"],
                "ledger_filled": ledger_coverage.get("filled", 0),
                "ledger_audited": ledger_coverage.get("audited", 0),
            },
            "score": {
                "intelligence_hit_rate": round(follow_correct / follow_done, 3) if follow_done else None,
                "ledger_fill_coverage": (
                    round(ledger_coverage["filled"] / ledger_coverage["decisions"], 3)
                    if ledger_coverage["decisions"]
                    else None
                ),
            },
            "top_sources": top_sources,
            "latency": _latency_stats(latency_samples, no_evidence=no_evidence),
            "suggestions": [
                {
                    "title": "继续保持 Shadow-only 验证链路",
                    "reason": "所有优化建议必须先进入 Replay/Backtest/Shadow/Paper，再人工批准。",
                    "stage": "SUGGESTION",
                    "can_apply": False,
                }
            ],
            "mae_mfe": False,
            "can_apply": False,
        }
        for row in session.reports.list(report_kind="optimization-daily", limit=1):
            extra = list((row.get("payload") or {}).get("suggestions") or [])
            extra.extend((row.get("payload") or {}).get("candidates") or [])
            report["suggestions"].extend(
                item for item in extra if item.get("can_apply") is False
            )
            report["pipeline"] = (row.get("payload") or {}).get("pipeline") or report.get("pipeline")
            report["trade_blocked"] = True
        report_id = session.reports.upsert(
            report_kind=self.report_kind,
            period_key=period_key,
            market=self.market,
            payload=report,
            created_at=current.isoformat(),
        )
        session.events.emit(
            "audit/report.generated",
            self.market,
            "bot",
            self.bot_name,
            {
                "report_id": report_id,
                "report_kind": self.report_kind,
                "period_key": period_key,
                "market": self.market,
                "item_count": len(items),
                "shadow_count": len(tasks),
            },
        )
        return report
