"""Gateway 阶段化错误契约。

Agent 必须依据 phase / submission_unknown 分类任务，不能只看 HTTP 状态码。
普通 503 可能发生在 venue 已接单之后。
"""

from __future__ import annotations

from typing import Literal
from uuid import uuid4

from fastapi import HTTPException

Phase = Literal["PRE_SUBMIT", "SUBMITTING", "VENUE", "POST_SUBMIT"]


def structured_error(
    status_code: int,
    *,
    error_code: str,
    phase: Phase,
    retryable: bool,
    submission_unknown: bool,
    message: str,
    request_id: str | None = None,
) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={
            "error_code": error_code,
            "phase": phase,
            "retryable": retryable,
            "submission_unknown": submission_unknown,
            "request_id": request_id or f"req-{uuid4().hex[:16]}",
            "message": message,
        },
    )
