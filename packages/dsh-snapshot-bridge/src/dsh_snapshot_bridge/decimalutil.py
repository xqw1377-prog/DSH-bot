"""金额和数量使用 Decimal 字符串，避免浮点误差。"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any


def decimal_string(value: Any, *, field: str) -> str:
    if value is None or value == "":
        raise ValueError(f"{field} is missing")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field} is not a decimal: {value!r}") from exc
    if not number.is_finite():
        raise ValueError(f"{field} is not finite: {value!r}")
    return format(number, "f")
