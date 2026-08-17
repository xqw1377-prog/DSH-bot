"""Bot 任务状态机（持久化）。

验收标准「Session 重启后任务状态不丢」：审批、下单等跨 tick 的
多阶段动作建模为任务，状态落 SQLite，重启后 Agent 从上次状态继续，
绝不因失忆重复发起审批或重复下单。

订单任务终态：仅在订单达到终态并完成账户对账后进入 RECONCILED。
SUBMITTED / ACKNOWLEDGED / FILLED 都不是任务终态。
"""

import json
from datetime import UTC, datetime

# 任务只能沿主链推进；终态不可逆
_TRANSITIONS: dict[str, set[str]] = {
    # 请求确定未到达交易系统（预览/审批/再校验失败）→ PRE_SUBMIT_FAILED
    "SIGNAL_RECEIVED": {"PREVIEWED", "FAILED", "PRE_SUBMIT_FAILED"},
    "PREVIEWED": {"AWAITING_APPROVAL", "FAILED", "PRE_SUBMIT_FAILED"},
    "AWAITING_APPROVAL": {
        "APPROVED_SUBMITTING", "REJECTED", "EXPIRED",
        "APPROVAL_UNKNOWN", "FAILED", "PRE_SUBMIT_FAILED",
    },
    # 已尝试提交但结果未知（网络错误/网关崩溃窗口）→ SUBMISSION_UNKNOWN：
    # 禁止重提，只能通过幂等键查询认领或确认失败
    "APPROVED_SUBMITTING": {"SUBMITTED", "FAILED", "SUBMISSION_UNKNOWN"},
    "SUBMISSION_UNKNOWN": {"SUBMITTED", "FAILED"},
    # 订单生命周期（对账前均非终态）
    # UNKNOWN 隔离超时可从任何在途订单状态直达 INCIDENT
    "SUBMITTED": {
        "ACKNOWLEDGED", "PARTIALLY_FILLED", "FILLED",
        "CANCELLED", "ORDER_REJECTED", "INCIDENT", "FAILED",
    },
    "ACKNOWLEDGED": {
        "PARTIALLY_FILLED", "FILLED", "CANCELLED", "ORDER_REJECTED",
        "INCIDENT", "FAILED",
    },
    "PARTIALLY_FILLED": {"PARTIALLY_FILLED", "FILLED", "CANCELLED", "FAILED"},
    # 成交与对账分离：FILLED + MATCHED → DONE；FILLED + MISMATCH → INCIDENT；
    # 对账数据暂不可用时自环保持 FILLED（reconciliation_status=PENDING）重试
    "FILLED": {"FILLED", "DONE", "INCIDENT", "RECONCILED", "FAILED"},
    "RECONCILED": set(),  # 兼容旧数据
    "FAILED": set(),
    "REJECTED": set(),
    "EXPIRED": set(),
    "CANCELLED": set(),
    "ORDER_REJECTED": set(),
    "APPROVAL_UNKNOWN": set(),
    "INCIDENT": set(),
    "PRE_SUBMIT_FAILED": set(),
    "DONE": set(),
}

TERMINAL = {
    "RECONCILED", "FAILED", "REJECTED", "EXPIRED",
    "CANCELLED", "ORDER_REJECTED", "DONE", "APPROVAL_UNKNOWN", "INCIDENT",
    "PRE_SUBMIT_FAILED",
}


class TaskError(ValueError):
    pass


class TaskStore:
    def __init__(self, bot: str, events=None):
        self.bot = bot
        self._events = events

    def create(self, kind: str, subject_id: str, payload: dict) -> str:
        from .store import _get

        task_id = f"task-{self.bot}-{subject_id}"
        _get().execute(
            "INSERT OR IGNORE INTO bot_tasks"
            " (task_id, bot, kind, status, subject_id, payload, created_at, updated_at)"
            " VALUES (?, ?, ?, 'SIGNAL_RECEIVED', ?, ?, ?, ?)",
            (task_id, self.bot, kind, subject_id,
             json.dumps(payload, ensure_ascii=False),
             datetime.now(UTC).isoformat(), datetime.now(UTC).isoformat()),
        )
        _get().commit()
        if self._events is not None:
            self._events.emit(
                "bot/task.created", "GLOBAL", "bot", self.bot,
                {"task_id": task_id, "kind": kind, "subject_id": subject_id,
                 "bot": self.bot},
            )
        return task_id

    def get(self, task_id: str) -> dict | None:
        from .store import _get

        row = _get().execute(
            "SELECT task_id, kind, status, subject_id, approval_id, order_id,"
            " idempotency_key, payload, created_at, updated_at,"
            " COALESCE(reconciliation_status, 'PENDING')"
            " FROM bot_tasks WHERE task_id = ? AND bot = ?",
            (task_id, self.bot),
        ).fetchone()
        if row is None:
            return None
        keys = ("task_id", "kind", "status", "subject_id", "approval_id",
                "order_id", "idempotency_key", "payload", "created_at",
                "updated_at", "reconciliation_status")
        task = dict(zip(keys, row))
        task["payload"] = json.loads(task["payload"])
        return task

    def find_by_status(self, *statuses: str) -> list[dict]:
        from .store import _get

        rows = _get().execute(
            "SELECT task_id FROM bot_tasks WHERE bot = ? AND status IN "
            f"({','.join('?' * len(statuses))}) ORDER BY created_at",
            (self.bot, *statuses),
        ).fetchall()
        return [t for t in (self.get(r[0]) for r in rows) if t]

    def transition(self, task_id: str, target: str, **updates) -> dict:
        """单步推进任务状态并回写字段（approval_id/order_id 等）。"""
        from .store import _get

        task = self.get(task_id)
        if task is None:
            raise TaskError(f"task not found: {task_id}")
        if target not in _TRANSITIONS[task["status"]]:
            raise TaskError(
                f"illegal task transition {task['status']} -> {target}"
            )
        now = datetime.now(UTC).isoformat()
        fields = {"status": target, "updated_at": now}
        fields.update({k: v for k, v in updates.items() if v is not None})
        assignments = ", ".join(f"{k} = ?" for k in fields)
        params = list(fields.values()) + [task_id, self.bot]
        _get().execute(
            f"UPDATE bot_tasks SET {assignments} WHERE task_id = ? AND bot = ?",
            params,
        )
        _get().commit()
        result = self.get(task_id)
        assert result is not None
        if self._events is not None:
            self._events.emit(
                "bot/task.transitioned", "GLOBAL", "bot", self.bot,
                {"task_id": task_id, "from": task["status"], "to": target,
                 "bot": self.bot},
            )
        return result
