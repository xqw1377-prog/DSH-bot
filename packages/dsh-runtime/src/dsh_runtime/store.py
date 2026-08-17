"""Agent 记忆与领域事件存储。

DSH Session 不能成为唯一交易账本（设计红线），但 Agent 的事件与
记忆必须持久化：Bot 主动巡检的结论、已处理的信号、发起的审批，
重启后都要能找回，否则定时任务会重复发起审批。
"""

import json
import os
import sqlite3
from pathlib import Path
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
            task_id         TEXT PRIMARY KEY,
            bot             TEXT NOT NULL,
            kind            TEXT NOT NULL,
            status          TEXT NOT NULL,
            subject_id      TEXT NOT NULL,
            approval_id     TEXT,
            order_id        TEXT,
            idempotency_key TEXT,
            payload         TEXT NOT NULL DEFAULT '{}',
            created_at       TEXT NOT NULL,
            updated_at       TEXT NOT NULL,
            reconciliation_status TEXT NOT NULL DEFAULT 'PENDING'
        );
        CREATE INDEX IF NOT EXISTS idx_tasks_bot_status ON bot_tasks (bot, status);
        """
    )
    # 迁移：旧库无 reconciliation_status 列时补上
    cols = {r[1] for r in conn.execute("PRAGMA table_info(bot_tasks)").fetchall()}
    if "reconciliation_status" not in cols:
        conn.execute(
            "ALTER TABLE bot_tasks ADD COLUMN reconciliation_status TEXT"
            " NOT NULL DEFAULT 'PENDING'"
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
    """领域事件日志，字段与 packages/event-schemas/envelope.json 对齐。

    若事件类型存在 payload schema（packages/event-schemas/<type>.json），
    发射前用 JSON Schema 校验：payload 与契约不符立即失败，
    而不是让坏事件流进账本。"""
    _validator_cache: dict[str, object] = {}

    @classmethod
    def _schema_dir(cls):
        # store.py 位于 <root>/packages/dsh-runtime/src/dsh_runtime/，
        # parents[3] 即 <root>/packages
        return Path(__file__).resolve().parents[3] / "event-schemas"

    @classmethod
    def _validator_for(cls, event_type: str):
        if event_type in cls._validator_cache:
            return cls._validator_cache[event_type]
        schema_file = cls._schema_dir() / f"{event_type}.json"
        validator = None
        if schema_file.exists():
            try:
                from jsonschema import Draft202012Validator, FormatChecker
                validator = Draft202012Validator(
                    json.loads(schema_file.read_text()),
                    format_checker=FormatChecker(),  # date-time 真校验
                )
            except ImportError:
                validator = None
        cls._validator_cache[event_type] = validator
        return validator

    def emit(self, event_type: str, market: str, actor_kind: str, actor_id: str,
             payload: dict) -> str:
        validator = self._validator_for(event_type)
        if validator is not None:
            errors = sorted(validator.iter_errors(payload), key=str)
            if errors:
                raise ValueError(
                    f"event {event_type} payload violates schema: "
                    f"{errors[0].message}"
                )
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
