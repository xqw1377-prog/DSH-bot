"""Runtime 本地 Outbox：六条验收 + 迁移 / 回滚 / 禁止直写 / live 门禁。"""

from datetime import UTC, datetime, timedelta
import sqlite3
import threading

import pytest

from dsh_contracts import Market
from dsh_runtime import (
    BotSession, Profile, TradeExecutionCore, outbox_metrics,
    publish_pending, reset, transaction,
)
from dsh_runtime.outbox import (
    MAX_ATTEMPTS, OutboxPublishCrash, backoff_seconds, consume_event,
    dlq_rows, pending_rows, publish_outbox, replay_audit_rows, replay_event,
    resolution_rows, skip_dead, terminate_aggregate,
)
from dsh_runtime.store import _get


@pytest.fixture(autouse=True)
def clean_store():
    reset()
    yield
    reset()


def _session(name: str = "t") -> BotSession:
    return BotSession.for_profile(Profile(
        name=name, description="", market="CRYPTO",
        primary_tools=frozenset(), prohibited=frozenset(),
    ))


def _requested(prefix: str, task_id: str | None = None) -> dict:
    payload = {
        "approval_id": f"appr-{prefix}",
        "market": "CRYPTO",
        "requested_by_bot": "t",
        "subject_type": "order",
        "subject_id": f"sig-{prefix}",
        "requested_at": "2026-01-01T00:00:00Z",
    }
    if task_id is not None:
        payload["task_id"] = task_id
    return payload


def test_commit_then_crash_before_publish_recovers(tmp_path, monkeypatch):
    db = tmp_path / "runtime.db"
    monkeypatch.setenv("DSH_RUNTIME_DB", str(db))
    monkeypatch.setenv("DSH_OUTBOX_SKIP_PUBLISH", "1")
    reset()
    session = _session()
    task_id = session.tasks.create("paper-order", "sig-crash", {"signal_id": "sig-crash"})
    assert session.tasks.get(task_id)["status"] == "SIGNAL_RECEIVED"
    assert session.events.query("bot/task.created") == []
    assert pending_rows(_get())

    reset()
    monkeypatch.delenv("DSH_OUTBOX_SKIP_PUBLISH")
    reset()
    session = _session()
    assert session.tasks.get(task_id)["status"] == "SIGNAL_RECEIVED"
    publish_pending()
    created = session.events.query("bot/task.created")
    assert len(created) == 1
    assert created[0]["payload"]["task_id"] == task_id


def test_publish_visible_before_ack_is_idempotent():
    session = _session()
    with transaction(publish=False):
        session.events.emit(
            "approval/requested", "CRYPTO", "bot", "t", _requested("ack")
        )
    conn = _get()
    with pytest.raises(OutboxPublishCrash, match="injected crash"):
        publish_outbox(conn, crash_after_publish=True, limit=1)
    assert len(session.events.query("approval/requested")) == 1
    rows = pending_rows(conn)
    assert any(r["status"] == "CLAIMED" for r in rows)

    past = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    conn.execute("UPDATE event_outbox SET locked_until = ?", (past,))
    conn.commit()
    publish_outbox(conn, owner="pub-b")
    assert len(session.events.query("approval/requested")) == 1
    assert all(r["status"] == "PUBLISHED" for r in pending_rows(conn))


def test_same_aggregate_sequence_constraint_and_failed_seq_blocks_next():
    session = _session()
    with transaction(publish=False):
        session.events.emit(
            "approval/requested", "CRYPTO", "bot", "t",
            _requested("s1", task_id="task-seq"),
        )
        e2 = session.events.emit(
            "approval/requested", "CRYPTO", "bot", "t",
            _requested("s2", task_id="task-seq"),
        )
        session.events.emit(
            "approval/requested", "CRYPTO", "bot", "t",
            _requested("s3", task_id="task-seq"),
        )
    conn = _get()
    rows = pending_rows(conn)
    assert [(r["aggregate_id"], r["sequence"]) for r in rows] == [
        ("task-seq", 1), ("task-seq", 2), ("task-seq", 3),
    ]
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO event_outbox"
            " (outbox_id, event_id, aggregate_id, sequence, event_type, occurred_at,"
            "  market, actor_kind, actor_id, payload, status)"
            " VALUES ('dup', 'dup-id', 'task-seq', 2, 'approval/requested',"
            " '2026-01-01T00:00:00Z', 'CRYPTO', 'bot', 't', '{}', 'PENDING')"
        )
    conn.rollback()

    publish_outbox(conn, limit=1)
    assert [e["payload"]["approval_id"] for e in session.events.query("approval/requested")] == [
        "appr-s1",
    ]

    t0 = datetime.now(UTC)
    publish_outbox(
        conn, fail_with=RuntimeError("seq-2-down"), fail_event_id=e2,
        now=t0, limit=1,
    )
    seq2 = next(r for r in pending_rows(conn) if r["event_id"] == e2)
    assert seq2["status"] == "PENDING"
    assert seq2["attempts"] == 1
    publish_outbox(conn, now=t0, limit=50)
    published = {e["payload"]["approval_id"] for e in session.events.query("approval/requested")}
    assert published == {"appr-s1"}
    assert not any(
        e["payload"]["approval_id"] == "appr-s3"
        for e in session.events.query("approval/requested")
    )


def test_failed_aggregate_does_not_block_other_aggregates():
    session = _session()
    with transaction(publish=False):
        e_poison = session.events.emit(
            "approval/requested", "CRYPTO", "bot", "t",
            _requested("poison", task_id="task-a"),
        )
        session.events.emit(
            "approval/requested", "CRYPTO", "bot", "t",
            _requested("a2", task_id="task-a"),
        )
        session.events.emit(
            "approval/requested", "CRYPTO", "bot", "t",
            _requested("b1", task_id="task-b"),
        )
    conn = _get()
    t0 = datetime.now(UTC)
    publish_outbox(
        conn, fail_with=RuntimeError("poison-a"), fail_event_id=e_poison,
        now=t0, limit=50,
    )
    ids = {e["payload"]["approval_id"] for e in session.events.query("approval/requested")}
    assert ids == {"appr-b1"}
    assert "appr-a2" not in ids


def test_backoff_then_dead_keeps_later_sequence_blocked():
    session = _session()
    with transaction(publish=False):
        e1 = session.events.emit(
            "approval/requested", "CRYPTO", "bot", "t",
            _requested("poison", task_id="task-dead"),
        )
        session.events.emit(
            "approval/requested", "CRYPTO", "bot", "t",
            _requested("later", task_id="task-dead"),
        )
        session.events.emit(
            "approval/requested", "CRYPTO", "bot", "t",
            _requested("other", task_id="task-other"),
        )
    conn = _get()
    base = datetime.now(UTC)
    for i in range(MAX_ATTEMPTS):
        now = base + timedelta(hours=i)
        publish_outbox(
            conn,
            fail_with=RuntimeError("poison"),
            fail_event_id=e1,
            now=now,
            limit=50,
        )
        row = next(r for r in pending_rows(conn) if r["event_id"] == e1)
        assert row["attempts"] == i + 1
        if i + 1 < MAX_ATTEMPTS:
            assert row["status"] == "PENDING"
            expected = now + timedelta(seconds=backoff_seconds(i + 1))
            assert row["available_at"] == expected.isoformat()
        else:
            assert row["status"] == "DEAD"

    dead = dlq_rows(conn)
    assert len(dead) == 1
    assert dead[0]["attempts"] == MAX_ATTEMPTS
    assert outbox_metrics(conn)["outbox_failed_count"] == 1

    publish_outbox(conn, now=base + timedelta(days=1))
    ids = {e["payload"]["approval_id"] for e in session.events.query("approval/requested")}
    assert "appr-later" not in ids
    assert "appr-other" in ids
    incidents = session.events.query("incident/opened")
    assert any("poison message" in e["payload"]["reason"] for e in incidents)
    assert outbox_metrics(conn)["outbox_failed_count"] == 1


def test_dead_unblocks_only_after_audited_skip():
    session = _session()
    with transaction(publish=False):
        e1 = session.events.emit(
            "approval/requested", "CRYPTO", "bot", "t",
            _requested("poison", task_id="task-skip"),
        )
        session.events.emit(
            "approval/requested", "CRYPTO", "bot", "t",
            _requested("later", task_id="task-skip"),
        )
    conn = _get()
    base = datetime.now(UTC)
    for i in range(MAX_ATTEMPTS):
        publish_outbox(
            conn, fail_with=RuntimeError("poison"), fail_event_id=e1,
            now=base + timedelta(hours=i), limit=1,
        )
    assert skip_dead(conn, e1, actor="operator") is True
    publish_outbox(conn, now=base + timedelta(days=1))
    ids = {e["payload"]["approval_id"] for e in session.events.query("approval/requested")}
    assert "appr-later" in ids
    actions = [r["action"] for r in resolution_rows(conn, "task-skip")]
    assert actions == ["skip"]


def test_dead_unblocks_only_after_audited_replay():
    session = _session()
    with transaction(publish=False):
        e1 = session.events.emit(
            "approval/requested", "CRYPTO", "bot", "t",
            _requested("poison", task_id="task-replay"),
        )
        session.events.emit(
            "approval/requested", "CRYPTO", "bot", "t",
            _requested("later", task_id="task-replay"),
        )
    conn = _get()
    base = datetime.now(UTC)
    for i in range(MAX_ATTEMPTS):
        publish_outbox(
            conn, fail_with=RuntimeError("poison"), fail_event_id=e1,
            now=base + timedelta(hours=i), limit=1,
        )
    assert replay_event(conn, e1, actor="operator") is True
    assert len([
        e for e in session.events.query("approval/requested")
        if e["payload"]["approval_id"] == "appr-poison"
    ]) == 1
    publish_outbox(conn, now=base + timedelta(days=1))
    ids = {e["payload"]["approval_id"] for e in session.events.query("approval/requested")}
    assert {"appr-poison", "appr-later"} <= ids
    actions = [r["action"] for r in resolution_rows(conn, "task-replay")]
    assert actions == ["replay"]


def test_terminate_aggregate_stops_remaining_sequences():
    session = _session()
    with transaction(publish=False):
        e1 = session.events.emit(
            "approval/requested", "CRYPTO", "bot", "t",
            _requested("poison", task_id="task-term"),
        )
        session.events.emit(
            "approval/requested", "CRYPTO", "bot", "t",
            _requested("later", task_id="task-term"),
        )
    conn = _get()
    base = datetime.now(UTC)
    for i in range(MAX_ATTEMPTS):
        publish_outbox(
            conn, fail_with=RuntimeError("poison"), fail_event_id=e1,
            now=base + timedelta(hours=i), limit=1,
        )
    assert terminate_aggregate(conn, "task-term", actor="operator", event_id=e1) >= 1
    publish_outbox(conn, now=base + timedelta(days=1))
    ids = {e["payload"]["approval_id"] for e in session.events.query("approval/requested")}
    assert "appr-later" not in ids
    actions = [r["action"] for r in resolution_rows(conn, "task-term")]
    assert actions == ["terminate"]
    assert all(
        r["status"] == "TERMINATED"
        for r in pending_rows(conn)
        if r["aggregate_id"] == "task-term"
    )


def test_lease_blocks_other_publisher_until_expiry():
    session = _session()
    with transaction(publish=False):
        session.events.emit(
            "approval/requested", "CRYPTO", "bot", "t", _requested("lease")
        )
    conn = _get()
    future = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
    conn.execute(
        "UPDATE event_outbox SET status = 'CLAIMED', lease_owner = 'pub-a',"
        " locked_until = ?",
        (future,),
    )
    conn.commit()
    assert publish_outbox(conn, owner="pub-b") == 0
    assert session.events.query("approval/requested") == []

    past = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    conn.execute("UPDATE event_outbox SET locked_until = ?", (past,))
    conn.commit()
    assert publish_outbox(conn, owner="pub-b") == 1
    claimed = next(r for r in pending_rows(conn))
    assert claimed["status"] == "PUBLISHED"
    assert claimed["locked_until"] is None
    assert len(session.events.query("approval/requested")) == 1


def test_two_publishers_cannot_double_claim(tmp_path, monkeypatch):
    db = tmp_path / "shared.db"
    monkeypatch.setenv("DSH_RUNTIME_DB", str(db))
    monkeypatch.setenv("DSH_OUTBOX_SKIP_PUBLISH", "1")
    reset()
    session = _session()
    session.events.emit(
        "approval/requested", "CRYPTO", "bot", "t", _requested("race")
    )
    reset()

    barrier = threading.Barrier(2)
    results: list[int] = []

    def worker(owner: str) -> None:
        conn = sqlite3.connect(db)
        barrier.wait()
        results.append(
            publish_outbox(conn, owner=owner, limit=1, skip_publish=False)
        )
        conn.close()

    threads = [
        threading.Thread(target=worker, args=("pub-a",)),
        threading.Thread(target=worker, args=("pub-b",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == [0, 1]

    monkeypatch.delenv("DSH_OUTBOX_SKIP_PUBLISH")
    reset()
    session = _session()
    assert len(session.events.query("approval/requested")) == 1


def test_replay_and_consume_are_idempotent():
    session = _session()
    event_id = session.events.emit(
        "approval/requested", "CRYPTO", "bot", "t", _requested("replay")
    )
    conn = _get()
    assert consume_event(conn, event_id, "projection") is True
    assert consume_event(conn, event_id, "projection") is False
    assert replay_event(conn, event_id, actor="auditor") is True
    assert replay_event(conn, event_id, actor="auditor") is True
    assert len(session.events.query("approval/requested")) == 1
    assert consume_event(conn, event_id, "projection") is False
    audits = replay_audit_rows(conn, event_id)
    assert len(audits) == 2
    assert {a["actor"] for a in audits} == {"auditor"}
    assert all(a["event_id"] == event_id for a in audits)


def test_schema_failure_rolls_back_task_and_outbox():
    session = _session()
    task_id = session.tasks.create("paper-order", "sig-rb", {"signal_id": "sig-rb"})
    with pytest.raises(ValueError, match="violates schema"):
        session.tasks.transition_with_event(
            task_id, "PREVIEWED",
            "order/submitted", "CRYPTO", "bot", "t",
            {"bad": "payload"},
        )
    assert session.tasks.get(task_id)["status"] == "SIGNAL_RECEIVED"
    assert session.events.query("order/submitted") == []
    assert not any(
        r["event_type"] == "bot/task.transitioned"
        and r["payload"].get("to") == "PREVIEWED"
        for r in pending_rows(_get())
    )

    with pytest.raises(ValueError, match="violates schema"):
        with transaction():
            session.tasks.transition(task_id, "PREVIEWED")
            session.events.emit(
                "order/submitted", "CRYPTO", "bot", "t", {"bad": "payload"}
            )
    assert session.tasks.get(task_id)["status"] == "SIGNAL_RECEIVED"


def test_outbox_down_does_not_write_domain_events(monkeypatch):
    session = _session()
    monkeypatch.setattr("dsh_runtime.store.outbox_ready", lambda _conn: False)
    with pytest.raises(RuntimeError, match="refuse direct"):
        session.events.emit(
            "approval/requested", "CRYPTO", "bot", "t", _requested("nope")
        )
    assert session.events.query("approval/requested") == []


def test_outbox_down_does_not_fallback_to_split_transition_emit(monkeypatch):
    session = _session()
    task_id = session.tasks.create("paper-order", "sig-no-obx", {"signal_id": "sig-no-obx"})
    monkeypatch.setattr("dsh_runtime.store.outbox_ready", lambda _conn: False)
    with pytest.raises(RuntimeError, match="refuse direct"):
        session.tasks.transition_with_event(
            task_id, "PREVIEWED",
            "approval/requested", "CRYPTO", "bot", "t",
            _requested("split", task_id=task_id),
        )
    assert session.tasks.get(task_id)["status"] == "SIGNAL_RECEIVED"
    assert session.events.query("approval/requested") == []
    assert session.events.query("bot/task.transitioned") == []


def test_old_sqlite_migrates_and_keeps_rows(tmp_path, monkeypatch):
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE bot_tasks (
            task_id TEXT PRIMARY KEY,
            bot TEXT NOT NULL,
            kind TEXT NOT NULL,
            status TEXT NOT NULL,
            subject_id TEXT NOT NULL,
            approval_id TEXT,
            order_id TEXT,
            idempotency_key TEXT,
            payload TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE domain_events (
            event_id TEXT PRIMARY KEY,
            event_type TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            market TEXT NOT NULL,
            actor_kind TEXT NOT NULL,
            actor_id TEXT NOT NULL,
            payload TEXT NOT NULL
        );
        """
    )
    conn.execute(
        "INSERT INTO bot_tasks"
        " (task_id, bot, kind, status, subject_id, payload, created_at, updated_at)"
        " VALUES (?, 't', 'paper-order', 'PREVIEWED', 'legacy',"
        " '{}', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')",
        ("task-t-legacy",),
    )
    conn.execute(
        "INSERT INTO domain_events VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("evt-legacy", "bot/task.created", "2026-01-01T00:00:00Z",
         "GLOBAL", "bot", "t",
         '{"task_id":"task-t-legacy","kind":"paper-order","subject_id":"legacy","bot":"t"}'),
    )
    conn.commit()
    conn.close()

    monkeypatch.setenv("DSH_RUNTIME_DB", str(db))
    reset()
    session = _session()
    task = session.tasks.get("task-t-legacy")
    assert task is not None
    assert task["status"] == "PREVIEWED"
    assert session.events.query("bot/task.created")[0]["event_id"] == "evt-legacy"
    session.events.emit(
        "approval/requested", "CRYPTO", "bot", "t", _requested("migrated")
    )
    assert len(session.events.query("approval/requested")) == 1


def test_live_mode_still_refuses_to_start():
    with pytest.raises(ValueError, match="live mode is disabled"):
        TradeExecutionCore(
            name="crypto-bot",
            market=Market.CRYPTO,
            gateway=object(),
            approvals=object(),
            account_id="acct",
            mode="live",
        )


def test_outbox_metrics_and_pending_skip(monkeypatch):
    monkeypatch.setenv("DSH_OUTBOX_SKIP_PUBLISH", "1")
    reset()
    session = _session()
    session.events.emit(
        "approval/requested", "CRYPTO", "bot", "t", _requested("metric")
    )
    metrics = outbox_metrics(_get())
    assert metrics["outbox_pending_count"] == 1
    assert metrics["outbox_oldest_pending_seconds"] is not None
    assert metrics["outbox_failed_count"] == 0
    assert session.events.query("approval/requested") == []
