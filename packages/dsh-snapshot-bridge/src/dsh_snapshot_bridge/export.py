"""导出编排：成功则原子替换；失败则保留上次账本并标记降级。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from dsh_snapshot_bridge.ashare import AShareSourceError, fetch_json, map_ashare_payloads
from dsh_snapshot_bridge.ashare_signals import map_policy_decisions
from dsh_snapshot_bridge.atomic import atomic_write_json
from dsh_snapshot_bridge.crypto import load_crypto_state, map_crypto_state
from dsh_snapshot_bridge.crypto_signals import (
    default_signals_path,
    load_jsonl_tail,
    map_crypto_rejects,
    map_crypto_signals,
)
from dsh_snapshot_bridge.crypto_trades import (
    map_crypto_closed_trades,
    map_crypto_equity_curve,
    map_crypto_fills,
)
from dsh_snapshot_bridge.schema import SCHEMA_VERSION, validate_snapshot
from dsh_snapshot_bridge.timeutil import utc_now


class ExportError(RuntimeError):
    def __init__(self, error_type: str, message: str):
        super().__init__(message)
        self.error_type = error_type


def _derive_cockpit_url(wallet_url: str) -> str:
    from urllib.parse import urlparse, urlunparse

    parsed = urlparse(wallet_url)
    if not parsed.scheme or not parsed.netloc:
        return ""
    return urlunparse((parsed.scheme, parsed.netloc, "/api/cockpit", "", "", ""))


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ExportError("CONFIG_MISSING", f"{name} is required")
    return value


def snapshot_path(directory: Path, market: str) -> Path:
    return directory / f"{market}.json"


def _load_existing(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def mark_stale(
    previous: dict[str, Any] | None,
    *,
    market: str,
    account_id: str,
    source_system: str,
    source_mode: str,
    error_type: str,
    detail: str,
) -> dict[str, Any]:
    exported = utc_now().isoformat()
    if previous and previous.get("accounts") and previous.get("market") == market:
        stale = dict(previous)
        stale["data_fresh"] = False
        stale["degraded"] = True
        stale["detail"] = detail
        stale["last_error_type"] = error_type
        stale["last_error_at"] = exported
        stale["exported_at"] = exported
        stale["source_observed_at"] = previous.get("source_observed_at")
        stale.setdefault("last_success_exported_at", previous.get("exported_at"))
        health = dict(stale.get("health") or {})
        health.update(
            {
                "system_ok": True,
                "data_fresh": False,
                "trading_channel_ok": False,
                "degraded": True,
                "detail": detail,
            }
        )
        stale["health"] = health
        return stale
    return {
        "schema_version": SCHEMA_VERSION,
        "snapshot_id": previous.get("snapshot_id") if previous else f"stale-{market}",
        "market": market,
        "account_id": account_id,
        "source_system": source_system,
        "source_mode": source_mode,
        "source_observed_at": (previous or {}).get("source_observed_at") or exported,
        "exported_at": exported,
        "data_fresh": False,
        "degraded": True,
        "detail": detail,
        "last_success_exported_at": (previous or {}).get("last_success_exported_at"),
        "last_error_type": error_type,
        "last_error_at": exported,
        "health": {
            "system_ok": False,
            "data_fresh": False,
            "trading_channel_ok": False,
            "clock_skew_ms": 0,
            "degraded": True,
            "detail": detail,
        },
        "accounts": [],
        "positions": [],
        "orders": [],
        "signals": [],
        "screen_results": [],
        "fills": [],
    }


def _write_validated(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    validate_snapshot(payload)
    atomic_write_json(path, payload)
    return payload


def _write_fail_closed(
    path: Path,
    payload: dict[str, Any],
    *,
    market: str,
    account_id: str,
    source_system: str,
    source_mode: str,
    error_type: str,
    detail: str,
) -> dict[str, Any]:
    try:
        return _write_validated(path, payload)
    except ValueError:
        empty = mark_stale(
            None,
            market=market,
            account_id=account_id,
            source_system=source_system,
            source_mode=source_mode,
            error_type=error_type,
            detail=detail,
        )
        return _write_validated(path, empty)


def export_crypto_snapshot(
    *,
    state_path: Path | None = None,
    output_dir: Path | None = None,
    account_id: str | None = None,
    source_system: str | None = None,
    source_mode: str | None = None,
    stale_after_seconds: int | None = None,
) -> dict[str, Any]:
    output = Path(output_dir or _require_env("QUANT_GATEWAY_SNAPSHOT_DIR"))
    dest = snapshot_path(output, "CRYPTO")
    account = account_id or os.environ.get("DSH_CRYPTO_ACCOUNT_ID") or os.environ.get(
        "PAPER_CRYPTO_ACCOUNT_ID", "paper-crypto-001"
    )
    system = source_system or os.environ.get("DSH_CRYPTO_SOURCE_SYSTEM", "6celue_v5")
    mode = source_mode or os.environ.get("DSH_CRYPTO_SOURCE_MODE", "unknown")
    stale_after = stale_after_seconds
    if stale_after is None:
        stale_after = int(os.environ.get("DSH_SNAPSHOT_STALE_SECONDS", "90"))
    previous = _load_existing(dest)
    try:
        path = Path(state_path or _require_env("DSH_CRYPTO_STATE_JSON"))
        state = load_crypto_state(path)
        signals_file = Path(
            os.environ.get("DSH_CRYPTO_SIGNALS_JSONL") or default_signals_path(path)
        )
        draft_id = f"crypto-{utc_now().isoformat()}"
        rows = load_jsonl_tail(signals_file)
        trades_file = Path(os.environ.get("DSH_CRYPTO_TRADES_JSONL") or (path.parent / "trades.jsonl"))
        equity_file = Path(
            os.environ.get("DSH_CRYPTO_EQUITY_JSONL") or (path.parent / "equity_snapshots.jsonl")
        )
        closed_trades = map_crypto_closed_trades(
            load_jsonl_tail(trades_file, max_lines=4000),
            account_id=account,
        )
        signals = map_crypto_signals(
            rows,
            strategy_version=str(
                state.get("runtime_version") or state.get("param_hash") or "6celue"
            ),
            snapshot_id=draft_id,
        )
        payload = map_crypto_state(
            state,
            account_id=account,
            source_system=system,
            source_mode=mode,
            stale_after_seconds=stale_after,
            signals=signals,
            rejected_candidates=map_crypto_rejects(rows),
            fills=map_crypto_fills(closed_trades),
            closed_trades=closed_trades,
            equity_curve=map_crypto_equity_curve(load_jsonl_tail(equity_file, max_lines=2000)),
        )
        for row in payload["signals"]:
            row["data_snapshot_id"] = payload["snapshot_id"]
        return _write_validated(dest, payload)
    except Exception as exc:
        error_type = getattr(exc, "error_type", None) or type(exc).__name__
        if isinstance(exc, FileNotFoundError):
            error_type = "CRYPTO_STATE_MISSING"
        elif "INVALID_JSON" in str(exc):
            error_type = "CRYPTO_STATE_INVALID_JSON"
        detail = f"source={system}; error={error_type}; {exc}"
        stale = mark_stale(
            previous,
            market="CRYPTO",
            account_id=account,
            source_system=system,
            source_mode=mode,
            error_type=error_type,
            detail=detail,
        )
        return _write_fail_closed(
            dest,
            stale,
            market="CRYPTO",
            account_id=account,
            source_system=system,
            source_mode=mode,
            error_type=error_type,
            detail=detail,
        )


def export_ashare_snapshot(
    *,
    wallet_url: str | None = None,
    screen_url: str | None = None,
    output_dir: Path | None = None,
    account_id: str | None = None,
    source_system: str | None = None,
    source_mode: str | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    output = Path(output_dir or _require_env("QUANT_GATEWAY_SNAPSHOT_DIR"))
    dest = snapshot_path(output, "A_SHARE")
    account = account_id or os.environ.get("DSH_A_SHARE_ACCOUNT_ID") or os.environ.get(
        "PAPER_A_SHARE_ACCOUNT_ID", "paper-a-share-001"
    )
    system = source_system or os.environ.get("DSH_A_SHARE_SOURCE_SYSTEM", "zisu")
    mode = source_mode or os.environ.get("DSH_A_SHARE_SOURCE_MODE", "paper")
    wallet = wallet_url or _require_env("DSH_A_SHARE_WALLET_URL")
    screen = screen_url or _require_env("DSH_A_SHARE_SCREEN_URL")
    seconds = timeout if timeout is not None else float(
        os.environ.get("DSH_SNAPSHOT_HTTP_TIMEOUT_SEC", "3")
    )
    previous = _load_existing(dest)
    try:
        wallet_payload = fetch_json(wallet, timeout=seconds)
        screen_payload = fetch_json(screen, timeout=seconds)
        signals: list = []
        cockpit_url = os.environ.get("DSH_A_SHARE_COCKPIT_URL", "").strip() or _derive_cockpit_url(
            wallet
        )
        if cockpit_url:
            try:
                cockpit = fetch_json(cockpit_url, timeout=seconds)
                signals = map_policy_decisions(
                    cockpit, snapshot_id=f"ashare-{utc_now().isoformat()}"
                )
            except Exception:
                signals = []
        payload = map_ashare_payloads(
            wallet_payload,
            screen_payload,
            account_id=account,
            source_system=system,
            source_mode=mode,
            signals=signals,
        )
        for row in payload["signals"]:
            row["data_snapshot_id"] = payload["snapshot_id"]
        return _write_validated(dest, payload)
    except Exception as exc:
        error_type = getattr(exc, "error_type", None) or type(exc).__name__
        if isinstance(exc, AShareSourceError):
            error_type = exc.error_type
        detail = f"source={system}; error={error_type}; {exc}"
        stale = mark_stale(
            previous,
            market="A_SHARE",
            account_id=account,
            source_system=system,
            source_mode=mode,
            error_type=error_type,
            detail=detail,
        )
        return _write_fail_closed(
            dest,
            stale,
            market="A_SHARE",
            account_id=account,
            source_system=system,
            source_mode=mode,
            error_type=error_type,
            detail=detail,
        )
