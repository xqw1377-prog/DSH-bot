"""Runtime 本地 Transactional Outbox。

只服务 packages/dsh-runtime 自己的 SQLite：
业务状态 + event_outbox 同一事务；Publisher 至少一次写入 domain_events。
不是 Gateway / Incident / Evolution Outbox，也不打开 live。
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from uuid import uuid4

LEASE_SECONDS = 30
MAX_ATTEMPTS = 5
MAX_BACKOFF_SECONDS = 60


class OutboxPublishCrash(RuntimeError):
    """测试注入：domain_events 已可见，但 outbox 尚未标记 PUBLISHED。"""


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None = None) -> str:
    return (value or _now()).isoformat()


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS event_outbox (
            outbox_id       TEXT PRIMARY KEY,
            event_id        TEXT NOT NULL UNIQUE,
            aggregate_id    TEXT NOT NULL,
            sequence        INTEGER NOT NULL,
            event_type      TEXT NOT NULL,
            occurred_at     TEXT NOT NULL,
            market          TEXT NOT NULL,
            actor_kind      TEXT NOT NULL,
            actor_id        TEXT NOT NULL,
            payload         TEXT NOT NULL,
            status          TEXT NOT NULL DEFAULT 'PENDING',
            attempts        INTEGER NOT NULL DEFAULT 0,
            available_at    TEXT,
            lease_owner     TEXT,
            locked_until    TEXT,
            last_error      TEXT,
            published_at    TEXT,
            UNIQUE (aggregate_id, sequence)
        );
        CREATE INDEX IF NOT EXISTS idx_outbox_status_seq
            ON event_outbox (status, aggregate_id, sequence);
        CREATE TABLE IF NOT EXISTS event_outbox_dlq (
            outbox_id    TEXT PRIMARY KEY,
            event_id     TEXT NOT NULL,
            aggregate_id TEXT NOT NULL,
            sequence     INTEGER NOT NULL,
            event_type   TEXT NOT NULL,
            payload      TEXT NOT NULL,
            failed_at    TEXT NOT NULL,
            last_error   TEXT,
            attempts     INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS event_consumption (
            event_id    TEXT NOT NULL,
            consumer    TEXT NOT NULL,
            consumed_at TEXT NOT NULL,
            PRIMARY KEY (event_id, consumer)
        );
        CREATE TABLE IF NOT EXISTS event_replay_audit (
            replay_id   TEXT PRIMARY KEY,
            event_id    TEXT NOT NULL,
            replayed_at TEXT NOT NULL,
            actor       TEXT NOT NULL
        );
        """
    )
    cols = {r[1] for r in conn.execute("PRAGMA table_info(event_outbox)").fetchall()}
    if "next_attempt_at" in cols and "available_at" not in cols:
        conn.execute(
            "ALTER TABLE event_outbox RENAME COLUMN next_attempt_at TO available_at"
        )
    if "lease_until" in cols and "locked_until" not in cols:
        conn.execute(
            "ALTER TABLE event_outbox RENAME COLUMN lease_until TO locked_until"
        )
    conn.execute("UPDATE event_outbox SET status = 'DEAD' WHERE status = 'FAILED'")
    conn.commit()


def outbox_ready(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='event_outbox'"
    ).fetchone()
    return row is not None


def aggregate_id_for(event_type: str, actor_id: str, payload: dict) -> str:
    if payload.get("task_id"):
        return str(payload["task_id"])
    if event_type.startswith("bot/tick"):
        return f"tick:{actor_id}"
    if event_type.startswith("bot/task"):
        return str(payload.get("task_id") or actor_id)
    return f"{event_type}:{actor_id}"


def enqueue(
    conn: sqlite3.Connection,
    *,
    event_type: str,
    market: str,
    actor_kind: str,
    actor_id: str,
    payload: dict,
    event_id: str | None = None,
) -> str:
    if not outbox_ready(conn):
        raise RuntimeError("outbox unavailable; refuse direct domain_events write")
    event_id = event_id or str(uuid4())
    aggregate_id = aggregate_id_for(event_type, actor_id, payload)
    occurred_at = _iso()
    seq_row = conn.execute(
        "SELECT COALESCE(MAX(sequence), 0) FROM event_outbox WHERE aggregate_id = ?",
        (aggregate_id,),
    ).fetchone()
    sequence = int(seq_row[0]) + 1
    conn.execute(
        "INSERT INTO event_outbox"
        " (outbox_id, event_id, aggregate_id, sequence, event_type, occurred_at,"
        "  market, actor_kind, actor_id, payload, status, available_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?)",
        (
            f"obx-{event_id}", event_id, aggregate_id, sequence, event_type,
            occurred_at, market, actor_kind, actor_id,
            json.dumps(payload, ensure_ascii=False), occurred_at,
        ),
    )
    return event_id


def _eligible_sql() -> str:
    return (
        "SELECT outbox_id, event_id, aggregate_id, sequence, event_type,"
        " occurred_at, market, actor_kind, actor_id, payload"
        " FROM event_outbox o"
        " WHERE o.status IN ('PENDING', 'CLAIMED')"
        " AND (o.available_at IS NULL OR o.available_at <= ?)"
        " AND (o.status = 'PENDING' OR o.locked_until IS NULL OR o.locked_until <= ?)"
        " AND o.sequence = ("
        "   SELECT MIN(sequence) FROM event_outbox o2"
        "   WHERE o2.aggregate_id = o.aggregate_id"
        "   AND o2.status NOT IN ('PUBLISHED', 'DEAD')"
        " )"
        " ORDER BY o.aggregate_id, o.sequence"
    )


def _claim(conn: sqlite3.Connection, outbox_id: str, owner: str, now: datetime) -> bool:
    until = _iso(now + timedelta(seconds=LEASE_SECONDS))
    now_iso = _iso(now)
    claimed = conn.execute(
        "UPDATE event_outbox SET status = 'CLAIMED', lease_owner = ?,"
        " locked_until = ?, attempts = attempts + 1"
        " WHERE outbox_id = ?"
        " AND status IN ('PENDING', 'CLAIMED')"
        " AND (status = 'PENDING' OR locked_until IS NULL OR locked_until <= ?)",
        (owner, until, outbox_id, now_iso),
    )
    return claimed.rowcount == 1


def backoff_seconds(attempts: int) -> int:
    return min(2 ** max(attempts, 0), MAX_BACKOFF_SECONDS)


def _backoff_iso(attempts: int, now: datetime) -> str:
    return _iso(now + timedelta(seconds=backoff_seconds(attempts)))


def _move_to_dlq(conn: sqlite3.Connection, row: tuple, error: str, now: datetime) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO event_outbox_dlq"
        " (outbox_id, event_id, aggregate_id, sequence, event_type, payload,"
        "  failed_at, last_error, attempts)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (row[0], row[1], row[2], row[3], row[4], row[9], _iso(now), error, MAX_ATTEMPTS),
    )
    conn.execute(
        "UPDATE event_outbox SET status = 'DEAD', last_error = ?,"
        " lease_owner = NULL, locked_until = NULL WHERE outbox_id = ?",
        (error, row[0]),
    )
    incident_payload = {
        "reason": f"runtime outbox poison message {row[1]} ({row[4]})",
        "task_id": json.loads(row[9]).get("task_id") if row[9] else None,
    }
    incident_payload = {k: v for k, v in incident_payload.items() if v is not None}
    enqueue(
        conn,
        event_type="incident/opened",
        market=row[6],
        actor_kind="system",
        actor_id="runtime-outbox",
        payload=incident_payload,
    )


def publish_outbox(
    conn: sqlite3.Connection,
    *,
    owner: str | None = None,
    now: datetime | None = None,
    limit: int = 50,
    skip_publish: bool | None = None,
    crash_after_publish: bool = False,
    fail_with: Exception | None = None,
    fail_event_id: str | None = None,
) -> int:
    """认领 PENDING（含过期 lease）并至少一次写入 domain_events。"""
    if skip_publish is None:
        skip_publish = os.environ.get("DSH_OUTBOX_SKIP_PUBLISH") == "1"
    if skip_publish:
        return 0
    if not outbox_ready(conn):
        raise RuntimeError("outbox unavailable; refuse direct domain_events write")
    owner = owner or f"pub-{uuid4().hex[:8]}"
    now = now or _now()
    published = 0
    remaining = limit
    while remaining > 0:
        now_iso = _iso(now)
        rows = conn.execute(_eligible_sql(), (now_iso, now_iso)).fetchall()
        if not rows:
            break
        progressed = False
        for row in rows[:remaining]:
            isolation = conn.isolation_level
            conn.isolation_level = None
            try:
                conn.execute("BEGIN IMMEDIATE")
                if not _claim(conn, row[0], owner, now):
                    conn.execute("ROLLBACK")
                    continue
                conn.execute("COMMIT")
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    if fail_with is not None and (
                        fail_event_id is None or row[1] == fail_event_id
                    ):
                        raise fail_with
                    conn.execute(
                        "INSERT OR IGNORE INTO domain_events"
                        " VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (row[1], row[4], row[5], row[6], row[7], row[8], row[9]),
                    )
                    conn.execute(
                        "INSERT OR IGNORE INTO event_consumption"
                        " (event_id, consumer, consumed_at) VALUES (?, 'domain_events', ?)",
                        (row[1], now_iso),
                    )
                    conn.execute("COMMIT")
                except OutboxPublishCrash:
                    raise
                except Exception as exc:
                    try:
                        conn.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                    _record_publish_failure(conn, row, exc, now)
                    continue
                if crash_after_publish:
                    raise OutboxPublishCrash(
                        "injected crash after domain_events publish"
                    )
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "UPDATE event_outbox SET status = 'PUBLISHED', published_at = ?,"
                    " last_error = NULL, lease_owner = NULL, locked_until = NULL"
                    " WHERE outbox_id = ?",
                    (now_iso, row[0]),
                )
                conn.execute("COMMIT")
                published += 1
                remaining -= 1
                progressed = True
            finally:
                conn.isolation_level = isolation
        if not progressed:
            break
    return published


def _record_publish_failure(
    conn: sqlite3.Connection, row: tuple, exc: Exception, now: datetime
) -> None:
    conn.execute("BEGIN IMMEDIATE")
    attempts = conn.execute(
        "SELECT attempts FROM event_outbox WHERE outbox_id = ?",
        (row[0],),
    ).fetchone()
    n = int(attempts[0]) if attempts else 0
    if n >= MAX_ATTEMPTS:
        _move_to_dlq(conn, row, str(exc), now)
    else:
        conn.execute(
            "UPDATE event_outbox SET status = 'PENDING',"
            " available_at = ?, last_error = ?,"
            " lease_owner = NULL, locked_until = NULL"
            " WHERE outbox_id = ?",
            (_backoff_iso(n, now), str(exc), row[0]),
        )
    conn.execute("COMMIT")


def replay_event(
    conn: sqlite3.Connection, event_id: str, *, actor: str = "runtime-outbox"
) -> bool:
    """按 event_id 安全重放：domain_events / 消费记录幂等，并写重放审计。"""
    if not outbox_ready(conn):
        raise RuntimeError("outbox unavailable; refuse direct domain_events write")
    row = conn.execute(
        "SELECT event_id, event_type, occurred_at, market, actor_kind, actor_id, payload"
        " FROM event_outbox WHERE event_id = ?",
        (event_id,),
    ).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT event_id, event_type, occurred_at, market, actor_kind, actor_id, payload"
            " FROM domain_events WHERE event_id = ?",
            (event_id,),
        ).fetchone()
    if row is None:
        return False
    conn.execute(
        "INSERT OR IGNORE INTO domain_events VALUES (?, ?, ?, ?, ?, ?, ?)",
        row,
    )
    conn.execute(
        "INSERT OR IGNORE INTO event_consumption"
        " (event_id, consumer, consumed_at) VALUES (?, 'domain_events', ?)",
        (event_id, _iso()),
    )
    conn.execute(
        "INSERT INTO event_replay_audit (replay_id, event_id, replayed_at, actor)"
        " VALUES (?, ?, ?, ?)",
        (str(uuid4()), event_id, _iso(), actor),
    )
    conn.commit()
    return True


def consume_event(conn: sqlite3.Connection, event_id: str, consumer: str) -> bool:
    """已消费事件再次消费返回 False，不产生副作用。"""
    cur = conn.execute(
        "INSERT OR IGNORE INTO event_consumption"
        " (event_id, consumer, consumed_at) VALUES (?, ?, ?)",
        (event_id, consumer, _iso()),
    )
    conn.commit()
    return cur.rowcount == 1


def pending_rows(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT event_id, event_type, status, aggregate_id, sequence, payload,"
        " attempts, available_at, locked_until"
        " FROM event_outbox ORDER BY aggregate_id, sequence"
    ).fetchall()
    return [
        {
            "event_id": r[0], "event_type": r[1], "status": r[2],
            "aggregate_id": r[3], "sequence": r[4],
            "payload": json.loads(r[5]),
            "attempts": r[6], "available_at": r[7], "locked_until": r[8],
        }
        for r in rows
    ]


def replay_audit_rows(conn: sqlite3.Connection, event_id: str | None = None) -> list[dict]:
    sql = "SELECT replay_id, event_id, replayed_at, actor FROM event_replay_audit"
    params: tuple = ()
    if event_id is not None:
        sql += " WHERE event_id = ?"
        params = (event_id,)
    sql += " ORDER BY replayed_at"
    return [
        {"replay_id": r[0], "event_id": r[1], "replayed_at": r[2], "actor": r[3]}
        for r in conn.execute(sql, params).fetchall()
    ]


def dlq_rows(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT event_id, event_type, last_error, attempts FROM event_outbox_dlq"
    ).fetchall()
    return [
        {
            "event_id": r[0], "event_type": r[1],
            "last_error": r[2], "attempts": r[3],
        }
        for r in rows
    ]


def outbox_metrics(conn: sqlite3.Connection) -> dict:
    pending = conn.execute(
        "SELECT COUNT(*), MIN(occurred_at) FROM event_outbox"
        " WHERE status IN ('PENDING', 'CLAIMED')"
    ).fetchone()
    failed = conn.execute(
        "SELECT COUNT(*) FROM event_outbox WHERE status = 'DEAD'"
    ).fetchone()
    oldest_seconds = None
    if pending and pending[1]:
        try:
            oldest = datetime.fromisoformat(pending[1])
            if oldest.tzinfo is None:
                oldest = oldest.replace(tzinfo=UTC)
            oldest_seconds = max(0.0, (_now() - oldest).total_seconds())
        except ValueError:
            oldest_seconds = None
    return {
        "outbox_pending_count": int(pending[0] or 0) if pending else 0,
        "outbox_oldest_pending_seconds": oldest_seconds,
        "outbox_failed_count": int(failed[0] or 0) if failed else 0,
    }
