"""DSH 只读快照契约与校验。"""

from __future__ import annotations

from typing import Any

from jsonschema import Draft202012Validator

from dsh_snapshot_bridge.secrets import assert_no_secrets
from dsh_snapshot_bridge.symbols import assert_no_symbol_collisions

SCHEMA_VERSION = "dsh-snapshot-1"

REQUIRED_META = (
    "schema_version",
    "snapshot_id",
    "market",
    "account_id",
    "source_system",
    "source_mode",
    "source_observed_at",
    "exported_at",
    "data_fresh",
    "degraded",
    "detail",
)

SNAPSHOT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": list(REQUIRED_META) + ["health", "accounts", "positions", "orders", "signals"],
    "properties": {
        "schema_version": {"const": SCHEMA_VERSION},
        "snapshot_id": {"type": "string", "minLength": 1},
        "market": {"enum": ["CRYPTO", "A_SHARE"]},
        "account_id": {"type": "string", "minLength": 1},
        "source_system": {"type": "string", "minLength": 1},
        "source_mode": {"type": "string", "minLength": 1},
        "source_observed_at": {"type": "string", "minLength": 1},
        "exported_at": {"type": "string", "minLength": 1},
        "data_fresh": {"type": "boolean"},
        "degraded": {"type": "boolean"},
        "detail": {"type": "string"},
        "last_success_exported_at": {"type": ["string", "null"]},
        "last_error_type": {"type": ["string", "null"]},
        "last_error_at": {"type": ["string", "null"]},
        "health": {
            "type": "object",
            "required": [
                "system_ok",
                "data_fresh",
                "trading_channel_ok",
                "degraded",
                "detail",
            ],
            "properties": {
                "system_ok": {"type": "boolean"},
                "data_fresh": {"type": "boolean"},
                "trading_channel_ok": {"const": False},
                "clock_skew_ms": {"type": "integer"},
                "degraded": {"type": "boolean"},
                "detail": {"type": "string"},
                "market_session": {"enum": ["OPEN", "CLOSED", "UNKNOWN"]},
            },
            "additionalProperties": True,
        },
        "accounts": {"type": "array", "items": {"type": "object"}},
        "positions": {"type": "array", "items": {"type": "object"}},
        "orders": {"type": "array", "items": {"type": "object"}},
        "signals": {"type": "array", "items": {"type": "object"}},
        "screen_results": {"type": "array"},
        "fills": {"type": "array"},
    },
    "additionalProperties": True,
}

_VALIDATOR = Draft202012Validator(SNAPSHOT_SCHEMA)


def validate_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    errors = sorted(_VALIDATOR.iter_errors(payload), key=lambda item: list(item.path))
    if errors:
        first = errors[0]
        location = ".".join(str(part) for part in first.path) or "$"
        raise ValueError(f"snapshot schema invalid at {location}: {first.message}")
    if payload.get("health", {}).get("trading_channel_ok") is not False:
        raise ValueError("snapshot trading_channel_ok must be false")
    for row in payload.get("signals") or []:
        if str(row.get("kind") or "").upper() == "SCREEN_RESULT":
            raise ValueError("SCREEN_RESULT must not be placed in signals")
    if payload["market"] == "A_SHARE":
        assert_no_symbol_collisions(list(payload.get("positions") or []))
    _assert_decimal_fields(payload)
    assert_no_secrets(payload)
    return payload


def _assert_decimal_fields(payload: dict[str, Any]) -> None:
    for account in payload.get("accounts") or []:
        for field in ("cash", "equity"):
            if field in account:
                _assert_decimal_string(account[field], f"accounts.{field}")
    for position in payload.get("positions") or []:
        for field in ("quantity", "available_quantity", "avg_cost"):
            if field in position:
                _assert_decimal_string(position[field], f"positions.{field}")


def _assert_decimal_string(value: Any, field: str) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a decimal string, got {type(value).__name__}")
    if value.lower() in {"nan", "inf", "-inf"}:
        raise ValueError(f"{field} is not a finite decimal")
