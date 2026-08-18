"""Runtime Local Transactional Outbox 验收。"""

import os
import re
import sqlite3
import threading
from pathlib import Path

import pytest

from dsh_runtime import (
    BotSession,
    Profile,
    outbox_metrics,
    pending_outbox,
    publish_outbox,
    reset,
    transaction,
)
from dsh_runtime.store import EventLog
from dsh_runtime.tasks import TaskStore

EXECUTION_SRC = (
    Path(__file__).resolve().parents[1] / "src" / "dsh_runtime" / "execution.py"
)


@pytest.fixture(autouse=True)
def clean_store():
    reset()
    yield
    reset()


def _session() -> BotSession:
    return BotSession.for_profile(Profile(
        name="outbox-bot", description="", market="CRYPTO",
        primary_tools=frozenset(), prohibited=frozenset(),
    ))


def _requested(approval_id: str) -> dict:
    return {
        "approval_id": approval_id, "market": "CRYPTO",
        "requested_by_bot": "outbox-bot", "subject_type": "order",
        "subject_id": f"sig-{approval_id}",
        "requested_at": "2026-01-01T00:00:00Z",
    }


def test_emit_publishes_to_domain_events():
    session = _session()
    session.events.emit(
        "approval/requested", "CRYPTO", "bot", "outbox-bot", _requested("appr-1")
    )
    assert len(session.events.query("approval/requested")) == 1
    assert pending_outbox()[0]["status"] == "PUBLISHED"


def test_crash_after_insert_before_ack_rolls_back_then_one_event(monkeypatch):
    monkeypatch.setenv("DSH_OUTBOX_SKIP_PUBLISH", "1")
    session = _session()
    session.events.emit(
        "approval/requested", "CRYPTO", "bot", "outbox-bot", _requested("appr-mid")
    )
    monkeypatch.delenv("DSH_OUTBOX_SKIP_PUBLISH")

    with pytest.raises(RuntimeError, match="after domain_events insert"):
        publish_outbox(crash_after_insert=True)

    assert session.events.query("approval/requested") == []
    assert pending_outbox()[0]["status"] == "PENDING"

    assert publish_outbox() == 1
    events = session.events.query("approval/requested")
    assert len(events) == 1
    assert {e["event_id"] for e in events} == {pending_outbox()[0]["event_id"]}


def test_two_publishers_one_domain_event(tmp_path, monkeypatch):
    db = tmp_path / "runtime-outbox.db"
    monkeypatch.setenv("DSH_RUNTIME_DB", str(db))
    monkeypatch.setenv("DSH_OUTBOX_SKIP_PUBLISH", "1")
    reset()
    session = _session()
    session.events.emit(
        "approval/requested", "CRYPTO", "bot", "outbox-bot", _requested("appr-race")
    )
    monkeypatch.delenv("DSH_OUTBOX_SKIP_PUBLISH")

    results: list[int] = []

    def worker():
        conn = sqlite3.connect(str(db), timeout=30.0, check_same_thread=False)
        conn.isolation_level = None
        try:
            results.append(publish_outbox(conn))
        finally:
            conn.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(results) == 1
    conn = sqlite3.connect(str(db))
    rows = conn.execute("SELECT event_id FROM domain_events").fetchall()
    conn.close()
    assert len(rows) == 1


def test_legacy_sqlite_migrates_event_outbox(tmp_path, monkeypatch):
    db = tmp_path / "legacy-runtime.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE agent_memory (
            note_id TEXT PRIMARY KEY, bot TEXT, kind TEXT,
            content TEXT, tags TEXT, created_at TEXT
        );
        CREATE TABLE domain_events (
            event_id TEXT PRIMARY KEY, event_type TEXT, occurred_at TEXT,
            market TEXT, actor_kind TEXT, actor_id TEXT, payload TEXT
        );
        CREATE TABLE bot_tasks (
            task_id TEXT PRIMARY KEY, bot TEXT, kind TEXT, status TEXT,
            subject_id TEXT, approval_id TEXT, order_id TEXT,
            idempotency_key TEXT, payload TEXT, created_at TEXT, updated_at TEXT
        );
        INSERT INTO bot_tasks VALUES (
            'task-legacy-sig-1', 'legacy-bot', 'paper-order', 'SIGNAL_RECEIVED',
            'sig-1', NULL, NULL, NULL, '{}', '2026-01-01T00:00:00Z',
            '2026-01-01T00:00:00Z'
        );
        INSERT INTO domain_events VALUES (
            'evt-legacy-1', 'bot/tick.started', '2026-01-01T00:00:00Z',
            'CRYPTO', 'system', 'legacy-bot', '{"bot":"legacy-bot"}'
        );
        """
    )
    conn.close()

    monkeypatch.setenv("DSH_RUNTIME_DB", str(db))
    reset()
    tasks = TaskStore(bot="legacy-bot")
    task = tasks.get("task-legacy-sig-1")
    assert task is not None
    assert task["status"] == "SIGNAL_RECEIVED"
    events = EventLog()
    assert events.query("bot/tick.started")[0]["event_id"] == "evt-legacy-1"
    events.emit(
        "approval/requested", "CRYPTO", "bot", "legacy-bot", {
            "approval_id": "appr-legacy", "market": "CRYPTO",
            "requested_by_bot": "legacy-bot", "subject_type": "order",
            "subject_id": "sig-1", "requested_at": "2026-01-01T00:00:00Z",
        }
    )
    assert events.query("approval/requested")


def test_execution_core_has_no_split_transition_emit():
    src = EXECUTION_SRC.read_text(encoding="utf-8")
    assert "with transaction():" not in src
    assert re.search(
        r"tasks\.transition\([^)]*\)\s*\n\s*session\.events\.emit", src
    ) is None
    assert "transition_with_event" in src


def test_outbox_metrics_expose_pending(monkeypatch):
    monkeypatch.setenv("DSH_OUTBOX_SKIP_PUBLISH", "1")
    session = _session()
    session.events.emit(
        "approval/requested", "CRYPTO", "bot", "outbox-bot", _requested("appr-m")
    )
    metrics = outbox_metrics()
    assert metrics["outbox_pending_count"] == 1
    assert metrics["outbox_failed_count"] == 0
    assert metrics["outbox_oldest_pending_seconds"] is not None
    assert metrics["outbox_oldest_pending_seconds"] >= 0


def test_task_and_event_roll_back_together():
    events = EventLog()
    tasks = TaskStore(bot="outbox-bot", events=events)
    task_id = tasks.create("paper-order", "sig-3", {"symbol": "BTCUSDT"})
    with pytest.raises(ValueError, match="no payload schema"):
        with transaction():
            tasks.transition(task_id, "PREVIEWED")
            events.emit("does/not.exist", "CRYPTO", "bot", "outbox-bot", {"x": 1})
    assert tasks.get(task_id)["status"] == "SIGNAL_RECEIVED"
    assert events.query("bot/task.transitioned") == []
