"""主动智能层一轮：采集结果入账 → Shadow → 复盘检查点 → 日报。

不启动 15 秒快照导出器。不审批、不下单。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from dsh_runtime.intelligence import (
    SIXCELUE_CRYPTO_UNIVERSE,
    BotIntelligenceJob,
    StrategyAuditorJob,
    holdings_from_snapshots,
    marks_from_snapshots,
)
from dsh_runtime.profile import load_profile
from dsh_runtime.session import BotSession
from dsh_runtime.snapshot_decisions import load_snapshot_signals, record_snapshot_decisions
from dsh_runtime.trade_audit import ingest_and_audit_trades, load_closed_trades


def _nominate_shadow_candidates(*market_sessions) -> dict:
    """Shadow 阶段候选提名到 strategy-evolution(只注册 DRAFT,不晋级)。

    环境变量门控:未配置 DSH_EVOLUTION_URL 则跳过(记录 skipped)。
    提名失败不杀循环——记入 errors,下一轮重试(幂等)。
    """
    import os

    from dsh_runtime.trade_audit import nominate_candidate_to_evolution

    url = os.environ.get("DSH_EVOLUTION_URL", "")
    api_key = os.environ.get("STRATEGY_EVOLUTION_API_KEY") or None
    result = {"enabled": bool(url), "nominated": [], "errors": []}
    if not url:
        result["skipped"] = "DSH_EVOLUTION_URL not configured"
        return result
    for session, market in market_sessions:
        for row in session.ledger.list_candidates(market=market, limit=20):
            if row.get("stage") != "SHADOW" or row.get("nominated_evolution_id"):
                continue
            try:
                outcome = nominate_candidate_to_evolution(
                    session, candidate_id=row["candidate_id"],
                    evolution_url=url, api_key=api_key)
                result["nominated"].append({
                    "candidate_id": row["candidate_id"],
                    "evolution_candidate_id": outcome["evolution_candidate_id"],
                    "market": market,
                })
            except Exception as exc:  # noqa: BLE001 提名失败不阻断自主循环
                result["errors"].append(f"{row['candidate_id']}: {exc}")
    return result


def run_autonomous_cycle(
    *,
    profiles_root: str | Path,
    ingest: Callable[[], dict[str, Any]] | None = None,
    snapshot_dir: str | Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    ingested = ingest() if ingest else {"skipped": True}
    current = now or datetime.now(UTC)
    holdings = holdings_from_snapshots(snapshot_dir)
    marks = marks_from_snapshots(snapshot_dir)
    root = Path(profiles_root)
    crypto_session = BotSession.for_profile(load_profile(root / "crypto-bot" / "profile.yaml"))
    ashare_session = BotSession.for_profile(load_profile(root / "a-stock-bot" / "profile.yaml"))
    crypto_job = BotIntelligenceJob(
        bot_name="crypto-bot",
        market="CRYPTO",
        source_env="DSH_CRYPTO_INTELLIGENCE_SOURCES",
        watchlist=SIXCELUE_CRYPTO_UNIVERSE,
    )
    ashare_job = BotIntelligenceJob(
        bot_name="a-stock-bot",
        market="A_SHARE",
        source_env="DSH_A_SHARE_INTELLIGENCE_SOURCES",
        watchlist=(),
    )
    crypto_signals = record_snapshot_decisions(
        crypto_session,
        market="CRYPTO",
        signals=load_snapshot_signals(snapshot_dir, "CRYPTO"),
    )
    ashare_signals = record_snapshot_decisions(
        ashare_session,
        market="A_SHARE",
        signals=load_snapshot_signals(snapshot_dir, "A_SHARE"),
    )
    crypto_items = crypto_job.run(
        crypto_session,
        holdings=holdings.get("CRYPTO"),
        marks=marks,
        now=current,
        snapshot_root=snapshot_dir,
    )
    ashare_items = ashare_job.run(
        ashare_session,
        holdings=holdings.get("A_SHARE"),
        marks=marks,
        now=current,
        snapshot_root=snapshot_dir,
    )
    crypto_trades = ingest_and_audit_trades(
        crypto_session,
        market="CRYPTO",
        trades=load_closed_trades(snapshot_dir, "CRYPTO"),
    )
    ashare_trades = ingest_and_audit_trades(
        ashare_session,
        market="A_SHARE",
        trades=load_closed_trades(snapshot_dir, "A_SHARE"),
    )
    nominations = _nominate_shadow_candidates(
        (crypto_session, "CRYPTO"), (ashare_session, "A_SHARE")
    )
    crypto_audit = StrategyAuditorJob(
        bot_name="crypto-bot", market="CRYPTO", report_kind="intelligence-daily"
    ).run(crypto_session, now=current)
    ashare_audit = StrategyAuditorJob(
        bot_name="a-stock-bot", market="A_SHARE", report_kind="intelligence-daily"
    ).run(ashare_session, now=current)
    return {
        "as_of": current.isoformat(),
        "mode": "SHADOW",
        "nominations": nominations,
        "ingest": {key: ingested.get(key) for key in ("documents", "events", "errors", "skipped") if key in ingested},
        "crypto_items": len(crypto_items),
        "ashare_items": len(ashare_items),
        "crypto_signals": crypto_signals,
        "ashare_signals": ashare_signals,
        "crypto_audit": crypto_audit.get("score"),
        "ashare_audit": ashare_audit.get("score"),
        "crypto_trades": {
            "linked": crypto_trades["linked"],
            "imported": crypto_trades["imported"],
            "audited": crypto_trades["audited"],
            "candidates": len(crypto_trades["candidates"]),
            "shadowed": len((crypto_trades.get("pipeline") or {}).get("shadowed") or []),
        },
        "ashare_trades": {
            "linked": ashare_trades["linked"],
            "imported": ashare_trades["imported"],
            "audited": ashare_trades["audited"],
            "candidates": len(ashare_trades["candidates"]),
        },
        "can_apply": False,
        "trade_blocked": True,
    }
