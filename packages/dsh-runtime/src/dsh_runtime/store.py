"""Agent 记忆与领域事件存储。

DSH Session 不能成为唯一交易账本（设计红线），但 Agent 的事件与
记忆必须持久化：Bot 主动巡检的结论、已处理的信号、发起的审批，
重启后都要能找回，否则定时任务会重复发起审批。
"""

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from datetime import UTC, datetime
from uuid import uuid4

from .outbox import enqueue, ensure_schema, outbox_ready, publish_outbox


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

        CREATE TABLE IF NOT EXISTS intelligence_items (
            item_id        TEXT PRIMARY KEY,
            bot            TEXT NOT NULL,
            market         TEXT NOT NULL,
            source_id      TEXT NOT NULL,
            symbol         TEXT,
            title          TEXT NOT NULL,
            source_url     TEXT,
            published_at   TEXT,
            observed_at    TEXT NOT NULL,
            authority      TEXT,
            direction      TEXT,
            horizon        TEXT,
            importance     REAL NOT NULL DEFAULT 0,
            confidence     REAL NOT NULL DEFAULT 0,
            action         TEXT,
            dedupe_key     TEXT NOT NULL,
            payload        TEXT NOT NULL DEFAULT '{}'
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_intel_bot_dedupe
            ON intelligence_items (bot, dedupe_key);
        CREATE INDEX IF NOT EXISTS idx_intel_bot_observed
            ON intelligence_items (bot, observed_at DESC);

        CREATE TABLE IF NOT EXISTS audit_reports (
            report_id      TEXT PRIMARY KEY,
            bot            TEXT NOT NULL,
            market         TEXT NOT NULL,
            report_kind    TEXT NOT NULL,
            period_key     TEXT NOT NULL,
            created_at     TEXT NOT NULL,
            payload        TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_audit_bot_period
            ON audit_reports (bot, report_kind, period_key);
        CREATE INDEX IF NOT EXISTS idx_audit_bot_created
            ON audit_reports (bot, created_at DESC);

        CREATE TABLE IF NOT EXISTS decision_ledger (
            decision_id TEXT PRIMARY KEY,
            bot TEXT NOT NULL,
            market TEXT NOT NULL,
            symbol TEXT,
            status TEXT NOT NULL,
            intel_grade TEXT NOT NULL,
            execution_lane TEXT NOT NULL,
            event_id TEXT,
            intelligence_item_id TEXT,
            signal_id TEXT,
            strategy_id TEXT,
            strategy_version TEXT,
            risk_snapshot_id TEXT,
            task_id TEXT,
            approval_id TEXT,
            order_id TEXT,
            fill_id TEXT,
            audit_id TEXT,
            capital_budget TEXT,
            max_risk TEXT,
            requires_approval INTEGER NOT NULL,
            can_apply INTEGER NOT NULL DEFAULT 0,
            direction TEXT,
            confidence TEXT,
            impact_horizon TEXT,
            action TEXT,
            entry_plan TEXT NOT NULL DEFAULT '{}',
            exit_plan TEXT NOT NULL DEFAULT '{}',
            evidence_refs TEXT NOT NULL DEFAULT '[]',
            payload TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_ledger_bot_link
            ON decision_ledger (bot, ifnull(event_id, ''), ifnull(signal_id, ''), ifnull(action, ''));
        CREATE INDEX IF NOT EXISTS idx_ledger_bot_updated
            ON decision_ledger (bot, updated_at DESC);

        CREATE TABLE IF NOT EXISTS position_episodes (
            episode_id    TEXT PRIMARY KEY,
            bot           TEXT NOT NULL,
            decision_id   TEXT NOT NULL,
            market        TEXT NOT NULL,
            symbol        TEXT NOT NULL,
            side          TEXT NOT NULL,
            entry_fill_id TEXT,
            entry_price   TEXT,
            entry_at      TEXT,
            quantity      TEXT NOT NULL DEFAULT '0',
            exit_fill_id  TEXT,
            exit_price    TEXT,
            exit_at       TEXT,
            exit_reason   TEXT,
            realized_pnl  TEXT,
            fees          TEXT NOT NULL DEFAULT '0',
            status        TEXT NOT NULL DEFAULT 'OPEN',
            outcomes      TEXT NOT NULL DEFAULT '{}',
            created_at    TEXT NOT NULL,
            updated_at    TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_episode_decision
            ON position_episodes (bot, decision_id);
        CREATE INDEX IF NOT EXISTS idx_episode_bot_updated
            ON position_episodes (bot, updated_at DESC);

        CREATE TABLE IF NOT EXISTS optimization_candidates (
            candidate_id TEXT PRIMARY KEY,
            bot          TEXT NOT NULL,
            market       TEXT NOT NULL,
            stage        TEXT NOT NULL,
            next_stage   TEXT,
            created_at   TEXT NOT NULL,
            updated_at   TEXT NOT NULL,
            payload      TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_candidate_bot_market
            ON optimization_candidates (bot, market, updated_at DESC);
        """
    )
    # 迁移：旧库无 reconciliation_status 列时补上
    cols = {r[1] for r in conn.execute("PRAGMA table_info(bot_tasks)").fetchall()}
    if "reconciliation_status" not in cols:
        conn.execute(
            "ALTER TABLE bot_tasks ADD COLUMN reconciliation_status TEXT"
            " NOT NULL DEFAULT 'PENDING'"
        )
    ledger_cols = {r[1] for r in conn.execute("PRAGMA table_info(decision_ledger)").fetchall()}
    if "approval_id" not in ledger_cols:
        conn.execute("ALTER TABLE decision_ledger ADD COLUMN approval_id TEXT")
    conn.commit()
    ensure_schema(conn)
    return conn


_conn: sqlite3.Connection | None = None
_tx_depth = 0


def _get() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = _connect()
    return _conn


def reset() -> None:
    """测试辅助：丢弃连接，恢复干净状态。"""
    global _conn, _tx_depth
    if _conn is not None:
        _conn.close()
        _conn = None
    _tx_depth = 0


def _commit_if_idle(conn: sqlite3.Connection) -> None:
    if _tx_depth == 0:
        conn.commit()


@contextmanager
def transaction(*, publish: bool = True):
    """嵌套安全事务：最外层 commit 后再 publish outbox。"""
    global _tx_depth
    conn = _get()
    outermost = _tx_depth == 0
    _tx_depth += 1
    try:
        yield conn
    except Exception:
        if outermost:
            conn.rollback()
        raise
    else:
        if outermost:
            conn.commit()
            if publish:
                publish_outbox(conn)
    finally:
        _tx_depth -= 1


def publish_pending() -> int:
    return publish_outbox(_get())


class Memory:
    """Bot 记忆：追加式笔记，供下一次 tick 和复盘读取。"""

    def __init__(self, bot: str):
        self.bot = bot

    def remember(self, content: str, kind: str = "note",
                 tags: list[str] | None = None) -> str:
        note_id = f"memo-{uuid4().hex[:12]}"
        conn = _get()
        conn.execute(
            "INSERT INTO agent_memory VALUES (?, ?, ?, ?, ?, ?)",
            (note_id, self.bot, kind, content,
             json.dumps(tags or [], ensure_ascii=False),
             datetime.now(UTC).isoformat()),
        )
        _commit_if_idle(conn)
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
        """去重判断：该 tag 是否已记录（如已处理过的 signal_id）。

        tags 是 JSON 数组,按元素精确匹配——LIKE '%tag%' 会把
        "sig-1" 误匹配到 "sig-10",以及含引号的 tag 永远匹配不上。
        """
        rows = _get().execute(
            "SELECT tags FROM agent_memory WHERE bot = ? AND tags LIKE ?",
            (self.bot, f'%"{tag}"%'),
        ).fetchall()
        for row in rows:
            try:
                tags = json.loads(row[0] or "[]")
            except (json.JSONDecodeError, TypeError):
                continue
            if tag in tags:
                return True
        return False


class IntelligenceStore:
    """结构化情报账本：来源、影响、建议和跟踪结果。"""

    def __init__(self, bot: str):
        self.bot = bot

    def upsert(
        self,
        *,
        dedupe_key: str,
        market: str,
        source_id: str,
        title: str,
        payload: dict,
        symbol: str | None = None,
        source_url: str | None = None,
        published_at: str | None = None,
        observed_at: str | None = None,
        authority: str | None = None,
        direction: str | None = None,
        horizon: str | None = None,
        importance: float = 0.0,
        confidence: float = 0.0,
        action: str | None = None,
    ) -> tuple[str, bool]:
        conn = _get()
        now = observed_at or datetime.now(UTC).isoformat()
        existing = conn.execute(
            "SELECT item_id FROM intelligence_items WHERE bot = ? AND dedupe_key = ?",
            (self.bot, dedupe_key),
        ).fetchone()
        if existing is not None:
            item_id = existing[0]
            prior = conn.execute(
                "SELECT observed_at, payload FROM intelligence_items WHERE item_id = ?",
                (item_id,),
            ).fetchone()
            prior_payload = json.loads(prior[1]) if prior and prior[1] else {}
            merged = {**payload}
            if prior_payload.get("follow_up"):
                merged["follow_up"] = prior_payload["follow_up"]
            merged["observed_at"] = prior_payload.get("observed_at") or (prior[0] if prior else now)
            conn.execute(
                "UPDATE intelligence_items SET market = ?, source_id = ?, symbol = ?,"
                " title = ?, source_url = ?, published_at = ?,"
                " authority = ?, direction = ?, horizon = ?, importance = ?,"
                " confidence = ?, action = ?, payload = ?"
                " WHERE item_id = ?",
                (
                    market,
                    source_id,
                    symbol,
                    title,
                    source_url,
                    published_at,
                    authority,
                    direction,
                    horizon,
                    float(importance),
                    float(confidence),
                    action,
                    json.dumps(merged, ensure_ascii=False),
                    item_id,
                ),
            )
            _commit_if_idle(conn)
            return item_id, False
        item_id = f"intel-{uuid4().hex[:12]}"
        conn.execute(
            "INSERT INTO intelligence_items"
            " (item_id, bot, market, source_id, symbol, title, source_url,"
            " published_at, observed_at, authority, direction, horizon,"
            " importance, confidence, action, dedupe_key, payload)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                item_id,
                self.bot,
                market,
                source_id,
                symbol,
                title,
                source_url,
                published_at,
                now,
                authority,
                direction,
                horizon,
                float(importance),
                float(confidence),
                action,
                dedupe_key,
                json.dumps(payload, ensure_ascii=False),
            ),
        )
        _commit_if_idle(conn)
        return item_id, True

    def list(
        self,
        *,
        market: str | None = None,
        limit: int = 50,
        min_importance: float | None = None,
    ) -> list[dict]:
        sql = (
            "SELECT item_id, market, source_id, symbol, title, source_url,"
            " published_at, observed_at, authority, direction, horizon,"
            " importance, confidence, action, payload"
            " FROM intelligence_items WHERE bot = ?"
        )
        params: list = [self.bot]
        if market:
            sql += " AND market = ?"
            params.append(market)
        if min_importance is not None:
            sql += " AND importance >= ?"
            params.append(float(min_importance))
        sql += " ORDER BY observed_at DESC LIMIT ?"
        params.append(limit)
        rows = _get().execute(sql, params).fetchall()
        return [
            {
                "item_id": r[0],
                "market": r[1],
                "source_id": r[2],
                "symbol": r[3],
                "title": r[4],
                "source_url": r[5],
                "published_at": r[6],
                "observed_at": r[7],
                "authority": r[8],
                "direction": r[9],
                "horizon": r[10],
                "importance": r[11],
                "confidence": r[12],
                "action": r[13],
                "payload": json.loads(r[14]) if r[14] else {},
            }
            for r in rows
        ]

    def update_payload(self, item_id: str, payload: dict) -> None:
        conn = _get()
        conn.execute(
            "UPDATE intelligence_items SET payload = ? WHERE item_id = ? AND bot = ?",
            (json.dumps(payload, ensure_ascii=False), item_id, self.bot),
        )
        _commit_if_idle(conn)


class AuditReportStore:
    """结构化审计报告：按 bot/period 去重，供 Projection 与 Chief 消费。"""

    def __init__(self, bot: str):
        self.bot = bot

    def upsert(
        self,
        *,
        report_kind: str,
        period_key: str,
        market: str,
        payload: dict,
        created_at: str | None = None,
    ) -> str:
        conn = _get()
        row = conn.execute(
            "SELECT report_id FROM audit_reports"
            " WHERE bot = ? AND report_kind = ? AND period_key = ?",
            (self.bot, report_kind, period_key),
        ).fetchone()
        now = created_at or datetime.now(UTC).isoformat()
        if row is not None:
            report_id = row[0]
            conn.execute(
                "UPDATE audit_reports SET market = ?, created_at = ?, payload = ?"
                " WHERE report_id = ?",
                (market, now, json.dumps(payload, ensure_ascii=False), report_id),
            )
            _commit_if_idle(conn)
            return report_id
        report_id = f"report-{uuid4().hex[:12]}"
        conn.execute(
            "INSERT INTO audit_reports"
            " (report_id, bot, market, report_kind, period_key, created_at, payload)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                report_id,
                self.bot,
                market,
                report_kind,
                period_key,
                now,
                json.dumps(payload, ensure_ascii=False),
            ),
        )
        _commit_if_idle(conn)
        return report_id

    def list(
        self,
        *,
        report_kind: str | None = None,
        limit: int = 20,
    ) -> list[dict]:
        sql = (
            "SELECT report_id, market, report_kind, period_key, created_at, payload"
            " FROM audit_reports WHERE bot = ?"
        )
        params: list = [self.bot]
        if report_kind:
            sql += " AND report_kind = ?"
            params.append(report_kind)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        rows = _get().execute(sql, params).fetchall()
        return [
            {
                "report_id": r[0],
                "market": r[1],
                "report_kind": r[2],
                "period_key": r[3],
                "created_at": r[4],
                "payload": json.loads(r[5]) if r[5] else {},
            }
            for r in rows
        ]


class DecisionLedger:
    """统一决策账本。没有关联的交易无法学习。"""

    def __init__(self, bot: str):
        self.bot = bot

    def upsert(self, record: dict) -> tuple[str, bool]:
        conn = _get()
        now = datetime.now(UTC).isoformat()
        event_id = record.get("event_id")
        signal_id = record.get("signal_id")
        action = record.get("action")
        existing = conn.execute(
            "SELECT decision_id FROM decision_ledger"
            " WHERE bot = ? AND ifnull(event_id,'') = ifnull(?,'')"
            " AND ifnull(signal_id,'') = ifnull(?,'') AND ifnull(action,'') = ifnull(?,'')",
            (self.bot, event_id, signal_id, action),
        ).fetchone()
        payload = {
            "decision_id": record.get("decision_id"),
            "bot": self.bot,
            "market": record["market"],
            "symbol": record.get("symbol"),
            "status": record.get("status") or "SHADOW",
            "intel_grade": record["intel_grade"],
            "execution_lane": record["execution_lane"],
            "event_id": event_id,
            "intelligence_item_id": record.get("intelligence_item_id"),
            "signal_id": signal_id,
            "strategy_id": record.get("strategy_id"),
            "strategy_version": record.get("strategy_version"),
            "risk_snapshot_id": record.get("risk_snapshot_id"),
            "task_id": record.get("task_id"),
            "approval_id": record.get("approval_id"),
            "order_id": record.get("order_id"),
            "fill_id": record.get("fill_id"),
            "audit_id": record.get("audit_id"),
            "capital_budget": record.get("capital_budget") or "0",
            "max_risk": record.get("max_risk") or "0",
            "requires_approval": 1 if record.get("requires_approval", True) else 0,
            "can_apply": 0,
            "direction": record.get("direction"),
            "confidence": str(record.get("confidence") or ""),
            "impact_horizon": record.get("impact_horizon"),
            "action": action,
            "entry_plan": json.dumps(record.get("entry_plan") or {}, ensure_ascii=False),
            "exit_plan": json.dumps(record.get("exit_plan") or {}, ensure_ascii=False),
            "evidence_refs": json.dumps(record.get("evidence_refs") or [], ensure_ascii=False),
            "payload": json.dumps(record.get("payload") or {}, ensure_ascii=False),
            "created_at": now,
            "updated_at": now,
        }
        if existing:
            decision_id = existing[0]
            prior = conn.execute(
                "SELECT fill_id, order_id, approval_id, audit_id, payload"
                " FROM decision_ledger WHERE decision_id = ?",
                (decision_id,),
            ).fetchone()
            fill_id = payload["fill_id"] or (prior[0] if prior else None)
            order_id = payload["order_id"] or (prior[1] if prior else None)
            approval_id = payload["approval_id"] or (prior[2] if prior else None)
            audit_id = payload["audit_id"] or (prior[3] if prior else None)
            prior_payload = json.loads(prior[4]) if prior and prior[4] else {}
            merged = {**prior_payload, **json.loads(payload["payload"])}
            if prior_payload.get("trade") and "trade" not in json.loads(payload["payload"]):
                merged["trade"] = prior_payload["trade"]
            conn.execute(
                """
                UPDATE decision_ledger SET
                    symbol=?, status=?, intel_grade=?, execution_lane=?,
                    intelligence_item_id=?, strategy_id=?, strategy_version=?,
                    risk_snapshot_id=?, task_id=?, approval_id=?, order_id=?, fill_id=?, audit_id=?,
                    capital_budget=?, max_risk=?, direction=?, confidence=?,
                    impact_horizon=?, entry_plan=?, exit_plan=?, evidence_refs=?,
                    payload=?, updated_at=?
                WHERE decision_id=?
                """,
                (
                    payload["symbol"],
                    payload["status"] if payload["status"] != "SHADOW" or not fill_id else "FILLED",
                    payload["intel_grade"],
                    payload["execution_lane"],
                    payload["intelligence_item_id"],
                    payload["strategy_id"],
                    payload["strategy_version"],
                    payload["risk_snapshot_id"],
                    payload["task_id"],
                    approval_id,
                    order_id,
                    fill_id,
                    audit_id,
                    payload["capital_budget"],
                    payload["max_risk"],
                    payload["direction"],
                    payload["confidence"],
                    payload["impact_horizon"],
                    payload["entry_plan"],
                    payload["exit_plan"],
                    payload["evidence_refs"],
                    json.dumps(merged, ensure_ascii=False),
                    now,
                    decision_id,
                ),
            )
            _commit_if_idle(conn)
            return decision_id, False
        decision_id = record.get("decision_id") or f"dec-{uuid4().hex[:12]}"
        conn.execute(
            """
            INSERT INTO decision_ledger (
                decision_id, bot, market, symbol, status, intel_grade, execution_lane,
                event_id, intelligence_item_id, signal_id, strategy_id, strategy_version,
                risk_snapshot_id, task_id, approval_id, order_id, fill_id, audit_id, capital_budget,
                max_risk, requires_approval, can_apply, direction, confidence,
                impact_horizon, action, entry_plan, exit_plan, evidence_refs, payload,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                decision_id,
                self.bot,
                payload["market"],
                payload["symbol"],
                payload["status"],
                payload["intel_grade"],
                payload["execution_lane"],
                payload["event_id"],
                payload["intelligence_item_id"],
                payload["signal_id"],
                payload["strategy_id"],
                payload["strategy_version"],
                payload["risk_snapshot_id"],
                payload["task_id"],
                payload["approval_id"],
                payload["order_id"],
                payload["fill_id"],
                payload["audit_id"],
                payload["capital_budget"],
                payload["max_risk"],
                payload["requires_approval"],
                payload["direction"],
                payload["confidence"],
                payload["impact_horizon"],
                payload["action"],
                payload["entry_plan"],
                payload["exit_plan"],
                payload["evidence_refs"],
                payload["payload"],
                now,
                now,
            ),
        )
        _commit_if_idle(conn)
        return decision_id, True

    def list(self, *, limit: int = 50) -> list[dict]:
        rows = _get().execute(
            "SELECT decision_id, market, symbol, status, intel_grade, execution_lane,"
            " event_id, intelligence_item_id, signal_id, strategy_id, strategy_version,"
            " risk_snapshot_id, task_id, approval_id, order_id, fill_id, audit_id, action,"
            " can_apply, entry_plan, exit_plan, evidence_refs, created_at, payload"
            " FROM decision_ledger WHERE bot = ? ORDER BY updated_at DESC LIMIT ?",
            (self.bot, limit),
        ).fetchall()
        return [self._row(row) for row in rows]

    def get(self, decision_id: str) -> dict | None:
        row = _get().execute(
            "SELECT decision_id, market, symbol, status, intel_grade, execution_lane,"
            " event_id, intelligence_item_id, signal_id, strategy_id, strategy_version,"
            " risk_snapshot_id, task_id, approval_id, order_id, fill_id, audit_id, action,"
            " can_apply, entry_plan, exit_plan, evidence_refs, created_at, payload"
            " FROM decision_ledger WHERE bot = ? AND decision_id = ?",
            (self.bot, decision_id),
        ).fetchone()
        return self._row(row) if row else None

    def find_by_signal(self, signal_id: str) -> dict | None:
        if not signal_id:
            return None
        row = _get().execute(
            "SELECT decision_id FROM decision_ledger WHERE bot = ? AND signal_id = ? ORDER BY updated_at DESC LIMIT 1",
            (self.bot, signal_id),
        ).fetchone()
        return self.get(row[0]) if row else None

    def find_by_fill(self, fill_id: str) -> dict | None:
        if not fill_id:
            return None
        row = _get().execute(
            "SELECT decision_id FROM decision_ledger"
            " WHERE bot = ? AND (fill_id = ? OR event_id = ?) ORDER BY updated_at DESC LIMIT 1",
            (self.bot, fill_id, fill_id),
        ).fetchone()
        return self.get(row[0]) if row else None

    def find_by_intelligence_item(self, item_id: str) -> dict | None:
        if not item_id:
            return None
        row = _get().execute(
            "SELECT decision_id FROM decision_ledger"
            " WHERE bot = ? AND intelligence_item_id = ? ORDER BY updated_at DESC LIMIT 1",
            (self.bot, item_id),
        ).fetchone()
        return self.get(row[0]) if row else None

    def attach_judgment(self, decision_id: str, judgment: dict) -> None:
        conn = _get()
        now = datetime.now(UTC).isoformat()
        row = conn.execute(
            "SELECT payload, exit_plan FROM decision_ledger WHERE decision_id = ? AND bot = ?",
            (decision_id, self.bot),
        ).fetchone()
        payload = json.loads(row[0]) if row and row[0] else {}
        exit_plan = json.loads(row[1]) if row and row[1] else {}
        payload["judgment"] = {**judgment, "can_apply": False}
        payload["follow_up"] = judgment.get("follow_up") or payload.get("follow_up")
        invalidation = list(exit_plan.get("invalidation") or [])
        for note in judgment.get("invalidation") or []:
            if note not in invalidation:
                invalidation.append(note)
        exit_plan["invalidation"] = invalidation
        conn.execute(
            "UPDATE decision_ledger SET payload = ?, exit_plan = ?, updated_at = ?"
            " WHERE decision_id = ? AND bot = ?",
            (
                json.dumps(payload, ensure_ascii=False),
                json.dumps(exit_plan, ensure_ascii=False),
                now,
                decision_id,
                self.bot,
            ),
        )
        _commit_if_idle(conn)

    def find_by_task(self, task_id: str) -> dict | None:
        if not task_id:
            return None
        row = _get().execute(
            "SELECT decision_id FROM decision_ledger"
            " WHERE bot = ? AND task_id = ? ORDER BY updated_at DESC LIMIT 1",
            (self.bot, task_id),
        ).fetchone()
        return self.get(row[0]) if row else None

    def _row(self, row: tuple) -> dict:
        payload = json.loads(row[23] or "{}")
        return {
            "decision_id": row[0],
            "market": row[1],
            "symbol": row[2],
            "status": row[3],
            "intel_grade": row[4],
            "execution_lane": row[5],
            "event_id": row[6],
            "intelligence_item_id": row[7],
            "signal_id": row[8],
            "strategy_id": row[9],
            "strategy_version": row[10],
            "risk_snapshot_id": row[11],
            "task_id": row[12],
            "approval_id": row[13],
            "order_id": row[14],
            "fill_id": row[15],
            "audit_id": row[16],
            "action": row[17],
            "can_apply": bool(row[18]),
            "entry_plan": json.loads(row[19] or "{}"),
            "exit_plan": json.loads(row[20] or "{}"),
            "evidence_refs": json.loads(row[21] or "[]"),
            "created_at": row[22],
            "episode_id": payload.get("episode_id"),
            "candidate_id": payload.get("candidate_id"),
            "payload": payload,
        }

    def attach_fill(self, decision_id: str, *, fill_id: str, order_id: str | None, trade: dict) -> None:
        conn = _get()
        now = datetime.now(UTC).isoformat()
        row = conn.execute(
            "SELECT payload FROM decision_ledger WHERE decision_id = ? AND bot = ?",
            (decision_id, self.bot),
        ).fetchone()
        payload = json.loads(row[0]) if row and row[0] else {}
        payload["trade"] = trade
        conn.execute(
            "UPDATE decision_ledger SET fill_id = ?, order_id = ?, status = ?, payload = ?, updated_at = ?"
            " WHERE decision_id = ? AND bot = ?",
            (fill_id, order_id, "FILLED", json.dumps(payload, ensure_ascii=False), now, decision_id, self.bot),
        )
        _commit_if_idle(conn)

    def attach_audit(self, decision_id: str, audit: dict) -> str:
        audit_id = str(audit.get("audit_id") or f"aud-{uuid4().hex[:12]}")
        conn = _get()
        now = datetime.now(UTC).isoformat()
        row = conn.execute(
            "SELECT payload FROM decision_ledger WHERE decision_id = ? AND bot = ?",
            (decision_id, self.bot),
        ).fetchone()
        payload = json.loads(row[0]) if row and row[0] else {}
        payload["audit"] = {**audit, "audit_id": audit_id}
        conn.execute(
            "UPDATE decision_ledger SET audit_id = ?, payload = ?, updated_at = ?"
            " WHERE decision_id = ? AND bot = ?",
            (audit_id, json.dumps(payload, ensure_ascii=False), now, decision_id, self.bot),
        )
        _commit_if_idle(conn)
        return audit_id

    def coverage(self) -> dict[str, int]:
        rows = self.list(limit=500)
        linked = [
            row
            for row in rows
            if (row.get("event_id") or row.get("signal_id"))
            and row.get("strategy_id")
        ]
        reconciled = 0
        for row in rows:
            reconciliation = (row.get("payload") or {}).get("reconciliation") or {}
            if reconciliation.get("reconciliation_status") == "MATCHED":
                reconciled += 1
        episodes = _get().execute(
            "SELECT status, COUNT(*) FROM position_episodes WHERE bot = ? GROUP BY status",
            (self.bot,),
        ).fetchall()
        episode_counts = {status: count for status, count in episodes}
        candidates = _get().execute(
            "SELECT COUNT(*) FROM optimization_candidates WHERE bot = ?",
            (self.bot,),
        ).fetchone()[0]
        return {
            "decisions": len(rows),
            "fully_linked": len(linked),
            "approved": sum(1 for row in rows if row.get("approval_id")),
            "ordered": sum(1 for row in rows if row.get("order_id")),
            "filled": sum(1 for row in rows if row.get("fill_id")),
            "reconciled": reconciled,
            "audited": sum(1 for row in rows if row.get("audit_id")),
            "episodes_open": episode_counts.get("OPEN", 0),
            "episodes_closed": episode_counts.get("CLOSED", 0),
            "candidates": candidates,
        }

    # ---- 持仓回合（PositionEpisode）：成交→持仓→退出与 1h/1d/3d 跟踪 ----

    OUTCOME_KEYS = ("1h", "1d", "3d")

    def open_episode(
        self,
        decision_id: str,
        *,
        market: str,
        symbol: str,
        side: str,
        entry_fill_id: str | None = None,
        entry_price: str | None = None,
        entry_at: str | None = None,
        quantity: str = "0",
    ) -> str:
        """为决策开启回合；同一决策已有回合（无论开闭）时幂等复用，不重复开。

        回合是结果跟踪的锚点：情报决策由此知道自己最后赚没赚钱。
        """
        conn = _get()
        now = datetime.now(UTC).isoformat()
        row = conn.execute(
            "SELECT episode_id FROM position_episodes"
            " WHERE bot = ? AND decision_id = ?"
            " ORDER BY created_at DESC LIMIT 1",
            (self.bot, decision_id),
        ).fetchone()
        if row:
            return row[0]
        episode_id = f"ep-{uuid4().hex[:12]}"
        conn.execute(
            """
            INSERT INTO position_episodes (
                episode_id, bot, decision_id, market, symbol, side,
                entry_fill_id, entry_price, entry_at, quantity,
                status, outcomes, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', '{}', ?, ?)
            """,
            (
                episode_id, self.bot, decision_id, market, symbol, side,
                entry_fill_id, entry_price, entry_at, quantity, now, now,
            ),
        )
        self._link_decision_payload(decision_id, episode_id=episode_id)
        _commit_if_idle(conn)
        return episode_id

    def close_episode(
        self,
        episode_id: str,
        *,
        exit_fill_id: str | None = None,
        exit_price: str | None = None,
        exit_at: str | None = None,
        exit_reason: str | None = None,
        realized_pnl: str | None = None,
        fees: str | None = None,
    ) -> dict | None:
        conn = _get()
        now = datetime.now(UTC).isoformat()
        episode = self.get_episode(episode_id)
        if episode is None:
            return None
        sets = ["status = 'CLOSED'", "updated_at = ?"]
        params: list = [now]
        for column, value in (
            ("exit_fill_id", exit_fill_id),
            ("exit_price", exit_price),
            ("exit_at", exit_at),
            ("exit_reason", exit_reason),
            ("realized_pnl", realized_pnl),
            ("fees", fees),
        ):
            if value is not None:
                sets.append(f"{column} = ?")
                params.append(value)
        params.append(episode_id)
        conn.execute(
            f"UPDATE position_episodes SET {', '.join(sets)} WHERE episode_id = ?",
            params,
        )
        _commit_if_idle(conn)
        return self.get_episode(episode_id)

    def record_episode_outcome(self, episode_id: str, key: str, value: str) -> dict | None:
        """回填 1h/1d/3d 观察结果；只接受事实观察值，不得编造。"""
        if key not in self.OUTCOME_KEYS:
            raise ValueError(f"outcome key must be one of {self.OUTCOME_KEYS}")
        conn = _get()
        now = datetime.now(UTC).isoformat()
        row = conn.execute(
            "SELECT outcomes FROM position_episodes WHERE episode_id = ? AND bot = ?",
            (episode_id, self.bot),
        ).fetchone()
        if row is None:
            return None
        outcomes = json.loads(row[0] or "{}")
        outcomes[key] = value
        conn.execute(
            "UPDATE position_episodes SET outcomes = ?, updated_at = ? WHERE episode_id = ?",
            (json.dumps(outcomes, ensure_ascii=False), now, episode_id),
        )
        _commit_if_idle(conn)
        return self.get_episode(episode_id)

    def get_episode(self, episode_id: str) -> dict | None:
        row = _get().execute(
            "SELECT episode_id, decision_id, market, symbol, side, entry_fill_id,"
            " entry_price, entry_at, quantity, exit_fill_id, exit_price, exit_at,"
            " exit_reason, realized_pnl, fees, status, outcomes, created_at, updated_at"
            " FROM position_episodes WHERE episode_id = ? AND bot = ?",
            (episode_id, self.bot),
        ).fetchone()
        return self._episode_row(row) if row else None

    def episodes_for_decision(self, decision_id: str) -> list[dict]:
        rows = _get().execute(
            "SELECT episode_id, decision_id, market, symbol, side, entry_fill_id,"
            " entry_price, entry_at, quantity, exit_fill_id, exit_price, exit_at,"
            " exit_reason, realized_pnl, fees, status, outcomes, created_at, updated_at"
            " FROM position_episodes WHERE bot = ? AND decision_id = ?"
            " ORDER BY created_at DESC",
            (self.bot, decision_id),
        ).fetchall()
        return [self._episode_row(row) for row in rows]

    @staticmethod
    def _episode_row(row: tuple) -> dict:
        return {
            "episode_id": row[0],
            "decision_id": row[1],
            "market": row[2],
            "symbol": row[3],
            "side": row[4],
            "entry_fill_id": row[5],
            "entry_price": row[6],
            "entry_at": row[7],
            "quantity": row[8],
            "exit_fill_id": row[9],
            "exit_price": row[10],
            "exit_at": row[11],
            "exit_reason": row[12],
            "realized_pnl": row[13],
            "fees": row[14],
            "status": row[15],
            "outcomes": json.loads(row[16] or "{}"),
            "created_at": row[17],
            "updated_at": row[18],
        }

    # ---- 优化候选（OptimizationCandidate）：带反事实数据，受控晋级 ----

    def save_candidate(self, candidate: dict) -> str:
        """幂等写入优化候选；candidate_id 由规则+周期决定，重跑不重复。"""
        candidate_id = str(candidate.get("candidate_id") or "")
        if not candidate_id:
            raise ValueError("candidate_id is required")
        conn = _get()
        now = datetime.now(UTC).isoformat()
        payload = {**candidate, "can_apply": False}
        existing = conn.execute(
            "SELECT created_at FROM optimization_candidates WHERE candidate_id = ?",
            (candidate_id,),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE optimization_candidates SET market = ?, stage = ?, next_stage = ?,"
                " payload = ?, updated_at = ? WHERE candidate_id = ?",
                (
                    candidate.get("market"), candidate.get("stage") or "SUGGESTION",
                    candidate.get("next_stage"), json.dumps(payload, ensure_ascii=False),
                    now, candidate_id,
                ),
            )
        else:
            conn.execute(
                """
                INSERT INTO optimization_candidates (
                    candidate_id, bot, market, stage, next_stage,
                    created_at, updated_at, payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate_id, self.bot, candidate.get("market"),
                    candidate.get("stage") or "SUGGESTION",
                    candidate.get("next_stage"), now, now,
                    json.dumps(payload, ensure_ascii=False),
                ),
            )
        _commit_if_idle(conn)
        return candidate_id

    def get_candidate(self, candidate_id: str) -> dict | None:
        row = _get().execute(
            "SELECT candidate_id, market, stage, next_stage, created_at, updated_at, payload"
            " FROM optimization_candidates WHERE candidate_id = ? AND bot = ?",
            (candidate_id, self.bot),
        ).fetchone()
        return self._candidate_row(row) if row else None

    def list_candidates(self, *, market: str | None = None, limit: int = 50) -> list[dict]:
        sql = (
            "SELECT candidate_id, market, stage, next_stage, created_at, updated_at, payload"
            " FROM optimization_candidates WHERE bot = ?"
        )
        params: list = [self.bot]
        if market:
            sql += " AND market = ?"
            params.append(market)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        rows = _get().execute(sql, params).fetchall()
        return [self._candidate_row(row) for row in rows]

    @staticmethod
    def _candidate_row(row: tuple) -> dict:
        payload = json.loads(row[6] or "{}")
        return {
            "candidate_id": row[0],
            "market": row[1],
            "stage": row[2],
            "next_stage": row[3],
            "created_at": row[4],
            "updated_at": row[5],
            **{k: v for k, v in payload.items() if k != "candidate_id"},
        }

    def _link_decision_payload(self, decision_id: str, **fields) -> None:
        """把链路 ID（episode_id/candidate_id）回填到决策 payload。"""
        conn = _get()
        now = datetime.now(UTC).isoformat()
        row = conn.execute(
            "SELECT payload FROM decision_ledger WHERE decision_id = ? AND bot = ?",
            (decision_id, self.bot),
        ).fetchone()
        if row is None:
            return
        payload = json.loads(row[0] or "{}")
        payload.update(fields)
        conn.execute(
            "UPDATE decision_ledger SET payload = ?, updated_at = ?"
            " WHERE decision_id = ? AND bot = ?",
            (json.dumps(payload, ensure_ascii=False), now, decision_id, self.bot),
        )


class EventLog:
    """领域事件日志，字段与 packages/event-schemas/envelope.json 对齐。

    若事件类型存在 payload schema（packages/event-schemas/<type>.json），
    发射前用 JSON Schema 校验：payload 与契约不符立即失败，
    而不是让坏事件流进账本。"""
    _validator_cache: dict[str, object] = {}

    _schema_dir_cache: Path | None = None

    @classmethod
    def _schema_dir(cls):
        """event-schemas 目录:源码树布局优先,wheel 安装回退包内副本。

        只依赖 parents[3] 的相对路径在 pip 安装(非 editable)后失效,
        会让所有 emit 抛 "no payload schema"。候选:
        1. 源码树 <root>/packages/event-schemas(editable 开发)
        2. 包内打包副本 dsh_runtime/event-schemas(wheel 安装)
        """
        import os

        if cls._schema_dir_cache is not None:
            return cls._schema_dir_cache
        env_dir = os.environ.get("DSH_EVENT_SCHEMAS_DIR")
        candidates = [
            Path(env_dir) if env_dir else None,
            Path(__file__).resolve().parents[3] / "event-schemas",
            Path(__file__).resolve().parent / "event-schemas",
        ]
        candidates = [c for c in candidates if c is not None]
        for candidate in candidates:
            if (candidate / "envelope.json").is_file():
                cls._schema_dir_cache = candidate
                return candidate
        raise ValueError(
            "event-schemas directory not found (source tree or package data)"
        )

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
        validator = Draft202012Validator(json.loads(schema_file.read_text(encoding="utf-8")))
        cls._validator_cache[event_type] = validator
        return validator

    def validate(self, event_type: str, payload: dict) -> None:
        validator = self._validator_for(event_type)
        errors = sorted(validator.iter_errors(payload), key=str)
        if errors:
            raise ValueError(
                f"event {event_type} payload violates schema: "
                f"{errors[0].message}"
            )

    def emit(self, event_type: str, market: str, actor_kind: str, actor_id: str,
             payload: dict) -> str:
        """只写入 event_outbox；禁止直写 domain_events。"""
        self.validate(event_type, payload)
        conn = _get()
        if not outbox_ready(conn):
            raise RuntimeError("outbox unavailable; refuse direct domain_events write")
        event_id = enqueue(
            conn,
            event_type=event_type,
            market=market,
            actor_kind=actor_kind,
            actor_id=actor_id,
            payload=payload,
        )
        if _tx_depth == 0:
            conn.commit()
            publish_outbox(conn)
        return event_id

    def publish_pending(self) -> int:
        return publish_pending()

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
