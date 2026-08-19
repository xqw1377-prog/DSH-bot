"""拒绝把密钥、Cookie、数据库连接写进快照。"""

from __future__ import annotations

import re
from typing import Any

FORBIDDEN_KEY_PARTS = (
    "api_key",
    "apikey",
    "api_secret",
    "secret",
    "password",
    "passwd",
    "cookie",
    "authorization",
    "bearer",
    "access_token",
    "refresh_token",
    "private_key",
    "dsn",
    "database_url",
    "connection_string",
    "conninfo",
)

_VALUE_HINTS = re.compile(
    r"(api[_-]?key|secret|password|bearer\s+[a-z0-9\-\._]+|mongodb(\+srv)?://|"
    r"postgres(ql)?://|mysql://|sqlite:)",
    re.IGNORECASE,
)


def assert_no_secrets(payload: Any, *, path: str = "$") -> None:
    if isinstance(payload, dict):
        for key, value in payload.items():
            lowered = str(key).lower()
            if any(part in lowered for part in FORBIDDEN_KEY_PARTS):
                raise ValueError(f"snapshot refuses secret field {path}.{key}")
            assert_no_secrets(value, path=f"{path}.{key}")
        return
    if isinstance(payload, list):
        for index, item in enumerate(payload):
            assert_no_secrets(item, path=f"{path}[{index}]")
        return
    if isinstance(payload, str) and _VALUE_HINTS.search(payload):
        raise ValueError(f"snapshot refuses secret-like value at {path}")
