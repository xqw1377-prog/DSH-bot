"""审计哈希链验收:追加可校验,篡改必被发现。"""

from quant_gateway import audit, storage


def _reset():
    storage.reset()


def test_chain_verifies_clean_log():
    _reset()
    for i in range(5):
        audit.record("order.submitted", service_principal="runtime",
                     market="A_SHARE", subject_id=f"ord-{i}",
                     detail=f"fill {i}")
    result = audit.verify_chain()
    assert result["ok"] is True
    assert result["chained_rows"] == 5
    assert result["legacy_rows"] == 0


def test_tampered_row_breaks_chain():
    _reset()
    for i in range(4):
        audit.record("order.submitted", service_principal="runtime",
                     detail=f"row {i}")
    # 篡改第 2 行的 detail(不改哈希)
    with storage.locked_conn() as conn:
        victim = conn.execute(
            "SELECT audit_id FROM audit_log WHERE entry_hash IS NOT NULL"
            " ORDER BY rowid LIMIT 1 OFFSET 1").fetchone()
        conn.execute(
            "UPDATE audit_log SET detail = '篡改后的内容' WHERE audit_id = ?",
            (victim[0],))
        conn.commit()
    result = audit.verify_chain()
    assert result["ok"] is False
    assert result["first_broken_audit_id"] == victim[0]


def test_deleted_row_breaks_chain():
    _reset()
    for i in range(3):
        audit.record("approval.decided", service_principal="bff",
                     detail=f"decision {i}")
    with storage.locked_conn() as conn:
        conn.execute(
            "DELETE FROM audit_log WHERE audit_id = ("
            " SELECT audit_id FROM audit_log WHERE entry_hash IS NOT NULL"
            " ORDER BY rowid LIMIT 1 OFFSET 1)")
        conn.commit()
    result = audit.verify_chain()
    assert result["ok"] is False


def test_legacy_rows_counted_not_verified():
    """建链前的历史行(无哈希)不参与校验,但链从首条新行正常延伸。"""
    _reset()
    with storage.locked_conn() as conn:
        conn.execute(
            "INSERT INTO audit_log (audit_id, occurred_at, action, outcome)"
            " VALUES ('audit-legacy-1', '2026-01-01T00:00:00+00:00',"
            " 'legacy.action', 'OK')")
        conn.commit()
    audit.record("order.submitted", service_principal="runtime", detail="new")
    audit.record("order.rejected", service_principal="runtime", detail="new2")
    result = audit.verify_chain()
    assert result["ok"] is True
    assert result["legacy_rows"] == 1
    assert result["chained_rows"] == 2


def test_event_schema_dir_fallback(tmp_path, monkeypatch):
    """schema 目录解析:环境变量 > 源码树 > 包内副本;缓存生效。"""
    from dsh_runtime.store import EventLog

    # 环境变量候选优先
    copy = tmp_path / "schemas"
    copy.mkdir()
    (copy / "envelope.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("DSH_EVENT_SCHEMAS_DIR", str(copy))
    EventLog._schema_dir_cache = None
    assert EventLog._schema_dir() == copy
    # 缓存:改环境变量不再影响(本进程内已解析)
    monkeypatch.setenv("DSH_EVENT_SCHEMAS_DIR", str(tmp_path / "nope"))
    assert EventLog._schema_dir() == copy
    EventLog._schema_dir_cache = None
    # 回退源码树
    monkeypatch.delenv("DSH_EVENT_SCHEMAS_DIR")
    resolved = EventLog._schema_dir()
    assert (resolved / "envelope.json").is_file()
