"""Bot 任务状态机（持久化）。

验收标准「Session 重启后任务状态不丢」：审批、下单等跨 tick 的
多阶段动作建模为任务，状态落 SQLite，重启后 Agent 从上次状态继续，
绝不因失忆重复发起审批或重复下单。

成功终态：FILLED + MATCHED → DONE。SUBMITTED / ACKNOWLEDGED / FILLED
都不是任务终态；对账失败或 UNKNOWN 超时进入 INCIDENT。
"""

import json
from datetime import UTC, datetime

# 任务只能沿主链推进；终态不可逆
_TRANSITIONS: dict[str, set[str]] = {
    # 请求确定未到达交易系统（预览/审批/再校验失败）→ PRE_SUBMIT_FAILED
    "SIGNAL_RECEIVED": {"PREVIEWED", "FAILED", "PRE_SUBMIT_FAILED"},
    "PREVIEWED": {"AWAITING_APPROVAL", "FAILED", "PRE_SUBMIT_FAILED", "SHADOW_RECORDED"},
    "AWAITING_APPROVAL": {
        "APPROVED_SUBMITTING", "REJECTED", "EXPIRED",
        "APPROVAL_UNKNOWN", "FAILED", "PRE_SUBMIT_FAILED",
    },
    # 已尝试提交但结果未知（网络错误/网关崩溃窗口）→ SUBMISSION_UNKNOWN：
    # 禁止重提，只能通过幂等键查询认领或确认失败
    "APPROVED_SUBMITTING": {
        "SUBMITTED", "FAILED", "SUBMISSION_UNKNOWN",
        "PRE_SUBMIT_FAILED", "PRE_SUBMIT_BLOCKED", "ORDER_REJECTED",
    },
    "PRE_SUBMIT_BLOCKED": {
        "APPROVED_SUBMITTING", "PRE_SUBMIT_FAILED", "SUBMISSION_UNKNOWN", "INCIDENT",
    },
    "SUBMISSION_UNKNOWN": {"SUBMITTED", "FAILED", "INCIDENT"},
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
    "PARTIALLY_FILLED": {
        "PARTIALLY_FILLED", "FILLED", "CANCELLED", "INCIDENT", "FAILED",
    },
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
    "SHADOW_RECORDED": set(),
    "DONE": set(),
}

TERMINAL = {
    "RECONCILED", "FAILED", "REJECTED", "EXPIRED",
    "CANCELLED", "ORDER_REJECTED", "DONE", "APPROVAL_UNKNOWN", "INCIDENT",
    "PRE_SUBMIT_FAILED", "SHADOW_RECORDED",
}


class TaskError(ValueError):
    pass


class TaskStore:
    def __init__(self, bot: str, events=None):
        self.bot = bot
        self._events = events

    def create(self, kind: str, subject_id: str, payload: dict) -> str:
        from .store import _get, transaction

        task_id = f"task-{self.bot}-{subject_id}"
        with transaction():
            _get().execute(
                "INSERT OR IGNORE INTO bot_tasks"
                " (task_id, bot, kind, status, subject_id, payload, created_at, updated_at)"
                " VALUES (?, ?, ?, 'SIGNAL_RECEIVED', ?, ?, ?, ?)",
                (task_id, self.bot, kind, subject_id,
                 json.dumps(payload, ensure_ascii=False),
                 datetime.now(UTC).isoformat(), datetime.now(UTC).isoformat()),
            )
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

    def update_payload(self, task_id: str, payload: dict, **fields) -> dict:
        """回写任务 payload（及可选的 idempotency_key 等），不改变状态。"""
        from .store import _get

        task = self.get(task_id)
        if task is None:
            raise TaskError(f"task not found: {task_id}")
        now = datetime.now(UTC).isoformat()
        updates = {"payload": json.dumps(payload, ensure_ascii=False), "updated_at": now}
        updates.update({k: v for k, v in fields.items() if v is not None})
        assignments = ", ".join(f"{k} = ?" for k in updates)
        params = list(updates.values()) + [task_id, self.bot]
        _get().execute(
            f"UPDATE bot_tasks SET {assignments} WHERE task_id = ? AND bot = ?",
            params,
        )
        from .store import _commit
        _commit()
        result = self.get(task_id)
        assert result is not None
        return result

    def transition(self, task_id: str, target: str, **updates) -> dict:
        """单步推进任务状态并回写字段（approval_id/order_id 等）。"""
        from .store import _get, transaction

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
        with transaction():
            _get().execute(
                f"UPDATE bot_tasks SET {assignments} WHERE task_id = ? AND bot = ?",
                params,
            )
            if self._events is not None:
                self._events.emit(
                    "bot/task.transitioned", "GLOBAL", "bot", self.bot,
                    {"task_id": task_id, "from": task["status"], "to": target,
                     "bot": self.bot},
                )
        result = self.get(task_id)
        assert result is not None
        return result

    def transition_with_event(
        self,
        task_id: str,
        target: str | None = None,
        extra_events: list[tuple] | None = None,
        payload: dict | None = None,
        **updates,
    ) -> dict | None:
        """任务状态 / payload 与额外领域事件同一事务提交。

        执行核禁止 `transition()` 后再单独 `EventLog.emit()`。
        extra_events 项为 (event_type, market, actor_kind, actor_id, payload)。
        """
        from .store import transaction

        extra_events = extra_events or []
        payload_fields = {}
        if payload is not None and "idempotency_key" in updates:
            payload_fields["idempotency_key"] = updates.pop("idempotency_key")
        with transaction():
            if payload is not None:
                self.update_payload(task_id, payload, **payload_fields)
            result = None
            if target is not None:
                result = self.transition(task_id, target, **updates)
            if self._events is not None:
                for event_type, market, actor_kind, actor_id, ev_payload in extra_events:
                    self._events.emit(
                        event_type, market, actor_kind, actor_id, ev_payload
                    )
        return result if result is not None else self.get(task_id)
