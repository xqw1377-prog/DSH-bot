"""A 股 .SH/.SZ/.BJ 映射：保留 source_symbol，DSH symbol 为 6 位代码。"""

from __future__ import annotations

import re
from typing import Any

_A_SHARE = re.compile(r"^(\d{6})\.(SH|SZ|BJ)$", re.IGNORECASE)


def normalize_ashare_symbol(source_symbol: str) -> tuple[str, str]:
    raw = str(source_symbol or "").strip().upper()
    if not raw:
        raise ValueError("source_symbol is empty")
    match = _A_SHARE.fullmatch(raw)
    if match:
        return match.group(1), f"{match.group(1)}.{match.group(2)}"
    if re.fullmatch(r"\d{6}", raw):
        return raw, raw
    raise ValueError(f"unsupported A-share symbol: {source_symbol!r}")


def restore_source_symbol(symbol: str, source_symbol: str) -> str:
    normalized, canonical_source = normalize_ashare_symbol(source_symbol)
    if normalized != symbol:
        raise ValueError(
            f"symbol/source_symbol mismatch: {symbol!r} vs {source_symbol!r}"
        )
    return canonical_source


def assert_no_symbol_collisions(rows: list[dict[str, Any]]) -> None:
    seen: dict[str, str] = {}
    for row in rows:
        symbol = str(row.get("symbol") or "")
        source = str(row.get("source_symbol") or "")
        previous = seen.get(symbol)
        if previous and previous != source:
            raise ValueError(
                f"A-share symbol collision: {symbol} maps from {previous} and {source}"
            )
        seen[symbol] = source


def normalize_crypto_symbol(source_symbol: str) -> tuple[str, str]:
    raw = str(source_symbol or "").strip().upper()
    if not raw:
        raise ValueError("crypto source_symbol is empty")
    return raw, raw
