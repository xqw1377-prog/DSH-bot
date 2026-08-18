"""Agent 记忆与领域事件存储。

DSH Session 不能成为唯一交易账本（设计红线），但 Agent 的事件与
记忆必须持久化：Bot 主动巡检的结论、已处理的信号、发起的审批，
重启后都要能找回，否则定时任务会重复发起审批。
"""

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
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

        CREATE TABLE IF NOT EXISTS event_outbox (
            outbox_id    TEXT PRIMARY KEY,
            event_id     TEXT NOT NULL UNIQUE,
            event_type   TEXT NOT NULL,
            occurred_at  TEXT NOT NULL,
            market       TEXT NOT NULL,
            actor_kind   TEXT NOT NULL,
            actor_id     TEXT NOT NULL,
            payload      TEXT NOT NULL,
            status       TEXT NOT NULL DEFAULT 'PENDING',
            attempts     INTEGER NOT NULL DEFAULT 0,
            last_error   TEXT,
            published_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_outbox_pending
            ON event_outbox (status, occurred_at);
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
_txn_depth = 0


def _get() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = _connect()
    return _conn


def _commit() -> None:
    """最外层才真正提交，以便任务状态与 outbox 同行。"""
    if _txn_depth == 0:
        _get().commit()


@contextmanager
def transaction():
    """嵌套安全的单元事务：提交后才发布 outbox。"""
    global _txn_depth
    _txn_depth += 1
    try:
        yield
        _txn_depth -= 1
        if _txn_depth == 0:
            _get().commit()
            publish_outbox()
    except Exception:
        _txn_depth -= 1
        if _txn_depth == 0:
            _get().rollback()
        raise


OUTBOX_MAX_ATTEMPTS = 8


def _publisher_conn() -> tuple[sqlite3.Connection, bool]:
    """发布器用独立连接，才能靠 BEGIN IMMEDIATE 做跨进程抢占。"""
    path = os.environ.get("DSH_RUNTIME_DB", ":memory:")
    if path not in {":memory:", ""}:
        conn = sqlite3.connect(path, timeout=30.0, check_same_thread=False)
        conn.isolation_level = None
        return conn, True
    return _get(), False


def publish_outbox(
    conn: sqlite3.Connection | None = None,
    *,
    crash_before_write: bool = False,
    crash_after_insert: bool = False,
    limit: int = 100,
) -> int:
    """把 PENDING outbox 写入 domain_events（至少一次）。

    每行：BEGIN IMMEDIATE 抢占 PENDING → INSERT OR IGNORE domain_events
    → 标记 PUBLISHED，同一事务提交。双发布器靠行抢占，不靠 Python 锁。
    """
    if os.environ.get("DSH_OUTBOX_SKIP_PUBLISH") == "1":
        return 0
    owns = False
    if conn is None:
        conn, owns = _publisher_conn()
    published = 0
    previous_isolation = conn.isolation_level
    try:
        conn.isolation_level = None
        if previous_isolation is not None:
            try:
                conn.execute("COMMIT")
            except sqlite3.OperationalError:
                pass
        rows = conn.execute(
            "SELECT outbox_id, event_id, event_type, occurred_at, market,"
            " actor_kind, actor_id, payload FROM event_outbox"
            " WHERE status = 'PENDING' ORDER BY occurred_at LIMIT ?",
            (limit,),
        ).fetchall()
        for row in rows:
            if crash_before_write:
                raise RuntimeError("injected crash before outbox publish")
            try:
                published += _publish_one(
                    conn, row, crash_after_insert=crash_after_insert
                )
            except RuntimeError:
                raise
            except Exception as exc:
                _mark_outbox_failure(conn, row[0], str(exc))
    finally:
        conn.isolation_level = previous_isolation
        if owns:
            conn.close()
    return published


def _publish_one(
    conn: sqlite3.Connection,
    row: tuple,
    *,
    crash_after_insert: bool,
) -> int:
    outbox_id = row[0]
    conn.execute("BEGIN IMMEDIATE")
    try:
        claimed = conn.execute(
            "UPDATE event_outbox SET status = 'PUBLISHING',"
            " attempts = attempts + 1"
            " WHERE outbox_id = ? AND status = 'PENDING'",
            (outbox_id,),
        )
        if claimed.rowcount != 1:
            conn.execute("ROLLBACK")
            return 0
        conn.execute(
            "INSERT OR IGNORE INTO domain_events VALUES (?, ?, ?, ?, ?, ?, ?)",
            row[1:8],
        )
        if crash_after_insert:
            raise RuntimeError("injected crash after domain_events insert")
        conn.execute(
            "UPDATE event_outbox SET status = 'PUBLISHED', published_at = ?,"
            " last_error = NULL WHERE outbox_id = ? AND status = 'PUBLISHING'",
            (datetime.now(UTC).isoformat(), outbox_id),
        )
        conn.execute("COMMIT")
        return 1
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise


def _mark_outbox_failure(conn: sqlite3.Connection, outbox_id: str, error: str) -> None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT attempts FROM event_outbox WHERE outbox_id = ?",
            (outbox_id,),
        ).fetchone()
        attempts = int(row[0]) if row else 0
        status = "FAILED" if attempts >= OUTBOX_MAX_ATTEMPTS else "PENDING"
        conn.execute(
            "UPDATE event_outbox SET status = ?, last_error = ?"
            " WHERE outbox_id = ?",
            (status, error, outbox_id),
        )
        conn.execute("COMMIT")
    except sqlite3.Error:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass


def outbox_metrics() -> dict:
    """Runtime 本地 outbox 基础指标，供查询接口暴露。"""
    conn = _get()
    pending = conn.execute(
        "SELECT COUNT(*), MIN(occurred_at) FROM event_outbox WHERE status = 'PENDING'"
    ).fetchone()
    failed = conn.execute(
        "SELECT COUNT(*) FROM event_outbox WHERE status = 'FAILED'"
    ).fetchone()
    oldest_seconds = None
    if pending and pending[1]:
        try:
            oldest = datetime.fromisoformat(pending[1])
            if oldest.tzinfo is None:
                oldest = oldest.replace(tzinfo=UTC)
            oldest_seconds = max(
                0.0, (datetime.now(UTC) - oldest).total_seconds()
            )
        except ValueError:
            oldest_seconds = None
    return {
        "outbox_pending_count": int(pending[0] or 0) if pending else 0,
        "outbox_oldest_pending_seconds": oldest_seconds,
        "outbox_failed_count": int(failed[0] or 0) if failed else 0,
    }


def pending_outbox() -> list[dict]:
    rows = _get().execute(
        "SELECT event_id, event_type, status, payload FROM event_outbox"
        " ORDER BY occurred_at"
    ).fetchall()
    return [
        {
            "event_id": r[0], "event_type": r[1], "status": r[2],
            "payload": json.loads(r[3]),
        }
        for r in rows
    ]


def reset() -> None:
    """测试辅助：丢弃连接，恢复干净状态。"""
    global _conn, _txn_depth
    if _conn is not None:
        _conn.close()
        _conn = None
    _txn_depth = 0


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
        _commit()
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
        if not schema_file.exists():
            raise ValueError(
                f"event {event_type} has no payload schema; refuse to emit"
            )
        try:
            from jsonschema import Draft202012Validator
        except ImportError as exc:
            raise ValueError(
                "jsonschema is required to emit domain events"
            ) from exc
        validator = Draft202012Validator(json.loads(schema_file.read_text()))
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
        occurred_at = datetime.now(UTC).isoformat()
        encoded = json.dumps(payload, ensure_ascii=False)
        _get().execute(
            "INSERT INTO event_outbox"
            " (outbox_id, event_id, event_type, occurred_at, market,"
            "  actor_kind, actor_id, payload, status)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PENDING')",
            (f"obx-{event_id}", event_id, event_type, occurred_at,
             market, actor_kind, actor_id, encoded),
        )
        if _txn_depth == 0:
            _get().commit()
            publish_outbox()
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
