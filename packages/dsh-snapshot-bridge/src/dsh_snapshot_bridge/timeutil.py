"""时间一律输出 UTC ISO 8601（带 +00:00）。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


def utc_now() -> datetime:
    return datetime.now(UTC)


def to_utc_iso(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).isoformat()
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e12:
            ts = ts / 1000.0
        if ts <= 0:
            return None
        return datetime.fromtimestamp(ts, tz=UTC).isoformat()
    text = str(value).strip()
    if not text:
        return None
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat()


def require_utc_iso(value: Any, *, field: str) -> str:
    converted = to_utc_iso(value)
    if converted is None:
        raise ValueError(f"{field} is not a usable timestamp")
    return converted
