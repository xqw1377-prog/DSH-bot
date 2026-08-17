"""Agent 记忆与领域事件存储。

DSH Session 不能成为唯一交易账本（设计红线），但 Agent 的事件与
记忆必须持久化：Bot 主动巡检的结论、已处理的信号、发起的审批，
重启后都要能找回，否则定时任务会重复发起审批。
"""

import json
import os
import sqlite3
from datetime import UTC, datetime
from uuid import uuid4


def _connect() -> sqlite3.Connection:
    path = os.environ.get("DSH_RUNTIME_DB", ":memory:")
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS agent_memory (
            note_id    TEXT PRIMARY KEY,
            bot        TEXT NOT NULL,
            kind       TEXT NOT NULL,
            content    TEXT NOT NULL,
            tags       TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_memory_bot ON agent_memory (bot, created_at);

        CREATE TABLE IF NOT EXISTS domain_events (
            event_id    TEXT PRIMARY KEY,
            event_type  TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            market      TEXT NOT NULL,
            actor_kind  TEXT NOT NULL,
            actor_id    TEXT NOT NULL,
            payload     TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_events_type ON domain_events (event_type, occurred_at);

        CREATE TABLE IF NOT EXISTS bot_tasks (
            task_id             TEXT PRIMARY KEY,
            bot                 TEXT NOT NULL,
            kind                TEXT NOT NULL,
            status              TEXT NOT NULL,
            reconciliation_status TEXT NOT NULL DEFAULT 'PENDING',
            subject_id          TEXT NOT NULL,
            approval_id         TEXT,
            order_id            TEXT,
            idempotency_key     TEXT,
            payload             TEXT NOT NULL DEFAULT '{}',
            created_at          TEXT NOT NULL,
            updated_at          TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_tasks_bot_status ON bot_tasks (bot, status);
        CREATE INDEX IF NOT EXISTS idx_tasks_reconcile ON bot_tasks (bot, reconciliation_status);

        -- Kill Switch HALTED 状态持久化（跨重启保持）
        CREATE TABLE IF NOT EXISTS halted_markets (
            market        TEXT PRIMARY KEY,
            incident_id   TEXT NOT NULL,
            halted_at     TEXT NOT NULL,
            resumed_at    TEXT,
            resume_approval_id TEXT,
            resumed_by    TEXT,
            resume_reason TEXT
        );

        -- Kill Switch 重试状态持久化（避免重启后丢失重试上下文）
        CREATE TABLE IF NOT EXISTS kill_switch_attempts (
            attempt_id     TEXT PRIMARY KEY,
            incident_id    TEXT NOT NULL,
            market         TEXT NOT NULL,
            violation_id   TEXT,
            attempt_no     INTEGER NOT NULL,
            status         TEXT NOT NULL,  -- REQUESTED | SUCCEEDED | FAILED | RETRYING
            last_error     TEXT,
            next_retry_at  TEXT,
            created_at     TEXT NOT NULL,
            updated_at     TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_attempts_incident ON kill_switch_attempts (incident_id);
        CREATE INDEX IF NOT EXISTS idx_attempts_retry ON kill_switch_attempts (status, next_retry_at);
        """
    )
    # 迁移：为已有 bot_tasks 表补 reconciliation_status 列（CREATE TABLE IF NOT
    # EXISTS 不会修改既有表结构）。SQLite 无 ADD COLUMN IF NOT EXISTS，用 pragma 探测。
    cols = {r[1] for r in conn.execute("PRAGMA table_info(bot_tasks)").fetchall()}
    if "reconciliation_status" not in cols:
        conn.execute(
            "ALTER TABLE bot_tasks ADD COLUMN reconciliation_status "
            "TEXT NOT NULL DEFAULT 'PENDING'"
        )
    conn.commit()
    return conn


_conn: sqlite3.Connection | None = None


def _get() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = _connect()
    return _conn


def reset() -> None:
    """测试辅助：丢弃连接，恢复干净状态。"""
    global _conn
    if _conn is not None:
        _conn.close()
        _conn = None


class Memory:
    """Bot 记忆：追加式笔记，供下一次 tick 和复盘读取。"""

    def __init__(self, bot: str):
        self.bot = bot

    def remember(self, content: str, kind: str = "note",
                 tags: list[str] | None = None) -> str:
        note_id = f"memo-{uuid4().hex[:12]}"
        _get().execute(
            "INSERT INTO agent_memory VALUES (?, ?, ?, ?, ?, ?)",
            (note_id, self.bot, kind, content,
             json.dumps(tags or [], ensure_ascii=False),
             datetime.now(UTC).isoformat()),
        )
        _get().commit()
        return note_id

    def recent(self, limit: int = 20, kind: str | None = None) -> list[dict]:
        sql = "SELECT note_id, kind, content, tags, created_at FROM agent_memory WHERE bot = ?"
        params: list = [self.bot]
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        rows = _get().execute(sql, params).fetchall()
        return [
            {
                "note_id": r[0], "kind": r[1], "content": r[2],
                "tags": json.loads(r[3]), "created_at": r[4],
            }
            for r in rows
        ]

    def has_tagged(self, tag: str) -> bool:
        """去重判断：该 tag 是否已记录（如已处理过的 signal_id）。"""
        row = _get().execute(
            "SELECT 1 FROM agent_memory WHERE bot = ? AND tags LIKE ? LIMIT 1",
            (self.bot, f'%"{tag}"%'),
        ).fetchone()
        return row is not None


class EventLog:
    """领域事件日志，字段与 packages/event-schemas/envelope.json 对齐。"""

    def emit(self, event_type: str, market: str, actor_kind: str, actor_id: str,
             payload: dict) -> str:
        event_id = str(uuid4())
        _get().execute(
            "INSERT INTO domain_events VALUES (?, ?, ?, ?, ?, ?, ?)",
            (event_id, event_type, datetime.now(UTC).isoformat(),
             market, actor_kind, actor_id, json.dumps(payload, ensure_ascii=False)),
        )
        _get().commit()
        return event_id

    def query(self, event_type: str | None = None, limit: int = 50) -> list[dict]:
        sql = "SELECT event_id, event_type, occurred_at, market, actor_kind, actor_id, payload FROM domain_events"
        params: list = []
        if event_type:
            sql += " WHERE event_type = ?"
            params.append(event_type)
        sql += " ORDER BY occurred_at DESC LIMIT ?"
        params.append(limit)
        rows = _get().execute(sql, params).fetchall()
        return [
            {
                "event_id": r[0], "event_type": r[1], "occurred_at": r[2],
                "market": r[3], "actor": {"kind": r[4], "id": r[5]},
                "payload": json.loads(r[6]),
            }
            for r in rows
        ]


class KillSwitchStore:
    """Kill Switch 状态持久化：HALTED 市场与重试尝试跨重启保持。

    设计：HALTED 状态必须跨 Incident Center / Gateway 重启保持，
    人工恢复必须带审批ID与审计记录，避免任何 Bot/LLM 自动恢复。
    """

    def is_halted(self, market: str) -> bool:
        row = _get().execute(
            "SELECT 1 FROM halted_markets WHERE market = ? AND resumed_at IS NULL",
            (market,),
        ).fetchone()
        return row is not None

    def halt(self, market: str, incident_id: str) -> bool:
        """把 market 标记为 HALTED。若已 HALTED 则返回 False（幂等）。"""
        now = datetime.now(UTC).isoformat()
        cur = _get().execute(
            "INSERT OR IGNORE INTO halted_markets "
            "(market, incident_id, halted_at) VALUES (?, ?, ?)",
            (market, incident_id, now),
        )
        _get().commit()
        return cur.rowcount > 0

    def resume(
        self, market: str, resumed_by: str, approval_id: str, reason: str,
    ) -> dict | None:
        """人工授权恢复：必须带审批ID与操作人。返回 HALTED 记录或 None。"""
        now = datetime.now(UTC).isoformat()
        cur = _get().execute(
            "UPDATE halted_markets SET resumed_at = ?, resumed_by = ?, "
            "resume_approval_id = ?, resume_reason = ? "
            "WHERE market = ? AND resumed_at IS NULL",
            (now, resumed_by, approval_id, reason, market),
        )
        _get().commit()
        if cur.rowcount == 0:
            return None
        row = _get().execute(
            "SELECT market, incident_id, halted_at, resumed_at, "
            "resume_approval_id, resumed_by, resume_reason "
            "FROM halted_markets WHERE market = ?",
            (market,),
        ).fetchone()
        return {
            "market": row[0], "incident_id": row[1], "halted_at": row[2],
            "resumed_at": row[3], "resume_approval_id": row[4],
            "resumed_by": row[5], "resume_reason": row[6],
        }

    def list_halted(self) -> list[dict]:
        rows = _get().execute(
            "SELECT market, incident_id, halted_at FROM halted_markets "
            "WHERE resumed_at IS NULL ORDER BY halted_at"
        ).fetchall()
        return [
            {"market": r[0], "incident_id": r[1], "halted_at": r[2]}
            for r in rows
        ]

    # ---- Kill Switch 重试状态 ----

    def record_attempt(
        self, incident_id: str, market: str, attempt_no: int,
        status: str, violation_id: str | None = None,
        last_error: str | None = None, next_retry_at: str | None = None,
    ) -> str:
        """记录一次 Kill Switch 尝试。返回 attempt_id。"""
        from uuid import uuid4
        attempt_id = f"ks-{uuid4().hex[:12]}"
        now = datetime.now(UTC).isoformat()
        _get().execute(
            "INSERT INTO kill_switch_attempts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (attempt_id, incident_id, market, violation_id, attempt_no,
             status, last_error, next_retry_at, now, now),
        )
        _get().commit()
        return attempt_id

    def update_attempt(
        self, attempt_id: str, status: str,
        last_error: str | None = None, next_retry_at: str | None = None,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        _get().execute(
            "UPDATE kill_switch_attempts SET status = ?, last_error = ?, "
            "next_retry_at = ?, updated_at = ? WHERE attempt_id = ?",
            (status, last_error, next_retry_at, now, attempt_id),
        )
        _get().commit()

    def last_attempt(self, incident_id: str) -> dict | None:
        row = _get().execute(
            "SELECT attempt_id, incident_id, market, violation_id, attempt_no, "
            "status, last_error, next_retry_at, created_at, updated_at "
            "FROM kill_switch_attempts WHERE incident_id = ? "
            "ORDER BY attempt_no DESC LIMIT 1",
            (incident_id,),
        ).fetchone()
        if not row:
            return None
        return {
            "attempt_id": row[0], "incident_id": row[1], "market": row[2],
            "violation_id": row[3], "attempt_no": row[4], "status": row[5],
            "last_error": row[6], "next_retry_at": row[7],
            "created_at": row[8], "updated_at": row[9],
        }

    def pending_retries(self) -> list[dict]:
        """获取状态为 RETRYING 且到期的尝试（用于退避重试）。"""
        now = datetime.now(UTC).isoformat()
        rows = _get().execute(
            "SELECT attempt_id, incident_id, market, violation_id, attempt_no "
            "FROM kill_switch_attempts WHERE status = 'RETRYING' "
            "AND next_retry_at IS NOT NULL AND next_retry_at <= ? "
            "ORDER BY next_retry_at",
            (now,),
        ).fetchall()
        return [
            {"attempt_id": r[0], "incident_id": r[1], "market": r[2],
             "violation_id": r[3], "attempt_no": r[4]}
            for r in rows
        ]
