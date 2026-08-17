"""Incident Center v2 测试。

测试覆盖：
1. 文本 incident/opened 事件只产生 HIGH 告警，不触发 Kill Switch
2. 只有 risk-policy 签发的 CRITICAL 事件才触发 Kill Switch
3. source != "risk-policy" 的事件被拒绝
4. HALTED 跨重启保持（持久化验证）
5. 人工恢复需审批ID + 操作人
6. Kill Switch 失败产生 kill_switch/failed 事件 + 退避重试
7. 达到重试上限产生人工告警
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from dsh_gateway_client import GatewayClient, RiskPolicyClient, RiskPolicyError
from dsh_runtime import BotSession, KillSwitchStore, Profile, reset
from dsh_incident_center import IncidentCenter, Severity, classify


@pytest.fixture(autouse=True)
def clean_store():
    reset()
    yield
    reset()


def _session() -> BotSession:
    return BotSession.for_profile(Profile(
        name="incident-center", description="", market="GLOBAL",
        primary_tools=frozenset({"incident_alert"}),
        prohibited=frozenset(),
    ))


def _gateway_stub() -> GatewayClient:
    """不实际发请求的 GatewayClient；kill_switch 用注入的回调替代。"""
    return GatewayClient.__new__(GatewayClient)


def _risk_policy_stub(violations: list[dict] | None = None,
                      fail: bool = False) -> RiskPolicyClient:
    """不实际发请求的 RiskPolicyClient。"""
    rp = MagicMock(spec=RiskPolicyClient)
    if fail:
        rp.list_critical_violations.side_effect = RiskPolicyError("unreachable")
    else:
        rp.list_critical_violations.return_value = violations or []
        rp.acknowledge.return_value = None
    return rp


def _make_critical_violation(
    market: str = "CRYPTO", rule_id: str = "MAX_POSITION_RATIO",
    violation_id: str = "rv-test-1", source: str = "risk-policy",
    severity: str = "CRITICAL",
) -> dict:
    return {
        "violation_id": violation_id,
        "severity": severity, "rule_id": rule_id, "market": market,
        "measured": 0.42, "limit": 0.30, "source": source,
        "account_id": None, "occurred_at": datetime.now(UTC).isoformat(),
        "acknowledged": False, "evidence_refs": [],
    }


# ---- 文本事件只能告警 ----

def test_classify_severity_text_only_returns_max_high():
    """文本分类最高 HIGH，不返回 CRITICAL。"""
    assert classify("emergency breach detected") == Severity.HIGH
    assert classify("position breach") == Severity.HIGH  # breach 文本也只 HIGH
    assert classify("market data degraded") == Severity.HIGH
    assert classify("市场降级") == Severity.HIGH
    assert classify("数据不一致") == Severity.HIGH
    assert classify("some unknown issue") == Severity.HIGH


def test_text_incident_does_not_trigger_kill_switch():
    """incident/opened 文本事件只产生告警，不触发 Kill Switch。"""
    triggered = []
    center = IncidentCenter(
        gateway=_gateway_stub(),
        risk_policy=_risk_policy_stub([]),
        kill_switch=lambda m, a: triggered.append((m, a)),
    )
    s = _session()
    s.events.emit(
        "incident/opened", "CRYPTO", "bot", "crypto-bot",
        {"reason": "emergency: position breach"},
    )
    center.tick(s)

    assert triggered == []
    # 应该产生 incident-alert 告警记忆
    alerts = s.memory.recent(kind="incident-alert")
    assert any("仅告警" in a["content"] for a in alerts)


# ---- risk-policy CRITICAL 事件触发 Kill Switch ----

def test_risk_policy_critical_triggers_kill_switch():
    """risk-policy 签发的 CRITICAL 事件触发 Kill Switch。"""
    triggered = []
    center = IncidentCenter(
        gateway=_gateway_stub(),
        risk_policy=_risk_policy_stub([_make_critical_violation()]),
        kill_switch=lambda m, a: triggered.append((m, a)),
    )
    s = _session()
    center.tick(s)

    assert len(triggered) == 1
    assert triggered[0][0] == "CRYPTO"
    # 应该产生 kill_switch/requested 与 kill_switch/succeeded 事件
    assert len(s.events.query("kill_switch/requested")) == 1
    assert len(s.events.query("kill_switch/succeeded")) == 1
    assert len(s.events.query("incident/mitigated")) == 1


def test_non_risk_policy_source_rejected():
    """source != 'risk-policy' 的事件被拒绝，不触发 Kill Switch。"""
    triggered = []
    v = _make_critical_violation(source="llm-agent")  # 伪造来源
    center = IncidentCenter(
        gateway=_gateway_stub(),
        risk_policy=_risk_policy_stub([v]),
        kill_switch=lambda m, a: triggered.append((m, a)),
    )
    s = _session()
    center.tick(s)

    assert triggered == []
    rejected = s.memory.recent(kind="kill-switch-rejected")
    assert any("source != risk-policy" in r["content"] for r in rejected)


def test_non_critical_severity_does_not_trigger():
    """severity != CRITICAL 不触发（即使是 risk-policy 签发）。"""
    triggered = []
    v = _make_critical_violation(severity="HIGH")
    center = IncidentCenter(
        gateway=_gateway_stub(),
        risk_policy=_risk_policy_stub([v]),
        kill_switch=lambda m, a: triggered.append((m, a)),
    )
    s = _session()
    center.tick(s)
    assert triggered == []


def test_risk_policy_unreachable_does_not_trigger():
    """risk-policy 不可达时不触发 Kill Switch（宁可漏触发也不由 LLM 触发）。"""
    triggered = []
    center = IncidentCenter(
        gateway=_gateway_stub(),
        risk_policy=_risk_policy_stub(fail=True),
        kill_switch=lambda m, a: triggered.append((m, a)),
    )
    s = _session()
    center.tick(s)
    assert triggered == []
    unreachable = s.memory.recent(kind="risk-policy-unreachable")
    assert len(unreachable) == 1


def test_no_risk_policy_client_does_not_trigger():
    """未配置 risk-policy 客户端时不能触发 Kill Switch。"""
    triggered = []
    center = IncidentCenter(
        gateway=_gateway_stub(),
        risk_policy=None,  # 未配置
        kill_switch=lambda m, a: triggered.append((m, a)),
    )
    s = _session()
    center.tick(s)
    assert triggered == []


# ---- HALTED 持久化 ----

def test_halted_persists_across_restart():
    """触发 Kill Switch 后，新建 IncidentCenter 实例仍看到 HALTED。"""
    triggered = []
    center1 = IncidentCenter(
        gateway=_gateway_stub(),
        risk_policy=_risk_policy_stub([_make_critical_violation()]),
        kill_switch=lambda m, a: triggered.append((m, a)),
    )
    s = _session()
    center1.tick(s)
    assert center1._is_halted("CRYPTO")

    # 模拟重启：新建实例（同 store，新内存状态）
    center2 = IncidentCenter(
        gateway=_gateway_stub(),
        risk_policy=_risk_policy_stub([]),  # 没有 violation
        kill_switch=lambda m, a: triggered.append((m, a)),
    )
    # 新实例应该看到 CRYPTO 仍 HALTED
    assert center2._is_halted("CRYPTO")
    assert any(h["market"] == "CRYPTO" for h in center2.list_halted())


def test_duplicate_violation_not_retriggered():
    """同一 violation_id 不重复触发。"""
    triggered = []
    v = _make_critical_violation()
    rp = _risk_policy_stub([v])
    center = IncidentCenter(
        gateway=_gateway_stub(),
        risk_policy=rp,
        kill_switch=lambda m, a: triggered.append((m, a)),
    )
    s = _session()
    center.tick(s)
    # risk-policy 已被 acknowledge，第二次 list 返回空
    rp.list_critical_violations.return_value = []
    center.tick(s)
    assert len(triggered) == 1


def test_halted_market_skips_new_critical():
    """HALTED 市场收到新 CRITICAL 不重复停。"""
    triggered = []
    v1 = _make_critical_violation(violation_id="rv-1")
    v2 = _make_critical_violation(violation_id="rv-2", rule_id="MAX_DRAWDOWN")
    center = IncidentCenter(
        gateway=_gateway_stub(),
        risk_policy=_risk_policy_stub([v1, v2]),
        kill_switch=lambda m, a: triggered.append((m, a)),
    )
    s = _session()
    center.tick(s)
    # 只触发一次（第二个 violation 因 HALTED 被跳过）
    assert len(triggered) == 1
    skipped = s.memory.recent(kind="kill-switch-skipped")
    assert any("HALTED" in m["content"] for m in skipped)


# ---- 人工授权恢复 ----

def test_resume_requires_approval_and_operator():
    """人工恢复必须带审批ID与操作人。"""
    center = IncidentCenter(
        gateway=_gateway_stub(),
        risk_policy=_risk_policy_stub([_make_critical_violation()]),
        kill_switch=lambda m, a: None,
    )
    s = _session()
    center.tick(s)
    assert center._is_halted("CRYPTO")

    record = center.resume_market(
        s, "CRYPTO", resumed_by="alice", approval_id="appr-001",
        reason="事故已修复，恢复交易",
    )
    assert record is not None
    assert record["resumed_by"] == "alice"
    assert record["resume_approval_id"] == "appr-001"
    assert not center._is_halted("CRYPTO")

    resumes = s.memory.recent(kind="manual-resume")
    assert any("alice" in r["content"] and "appr-001" in r["content"] for r in resumes)


def test_resume_non_halted_returns_none():
    """恢复未 HALTED 的市场返回 None。"""
    center = IncidentCenter(
        gateway=_gateway_stub(),
        risk_policy=_risk_policy_stub([]),
        kill_switch=lambda m, a: None,
    )
    s = _session()
    record = center.resume_market(
        s, "CRYPTO", resumed_by="alice", approval_id="appr-001",
        reason="test",
    )
    assert record is None


def test_resume_persists_across_restart():
    """恢复操作也跨重启保持。"""
    center1 = IncidentCenter(
        gateway=_gateway_stub(),
        risk_policy=_risk_policy_stub([_make_critical_violation()]),
        kill_switch=lambda m, a: None,
    )
    s = _session()
    center1.tick(s)
    center1.resume_market(
        s, "CRYPTO", resumed_by="alice", approval_id="appr-001", reason="fixed",
    )

    center2 = IncidentCenter(
        gateway=_gateway_stub(),
        risk_policy=_risk_policy_stub([]),
        kill_switch=lambda m, a: None,
    )
    assert not center2._is_halted("CRYPTO")


# ---- Kill Switch 失败 + 退避重试 ----

def test_kill_switch_failure_emits_failed_event_and_retries():
    """Kill Switch 失败产生 kill_switch/failed 事件，并安排重试。"""
    calls = []
    def failing_kill(m, a):
        calls.append((m, a, datetime.now(UTC)))
        raise RuntimeError("gateway down")

    v = _make_critical_violation()
    center = IncidentCenter(
        gateway=_gateway_stub(),
        risk_policy=_risk_policy_stub([v]),
        kill_switch=failing_kill,
    )
    s = _session()
    center.tick(s)

    # 第一次失败：attempt=1，will_retry=True
    failed_events = s.events.query("kill_switch/failed")
    assert len(failed_events) == 1
    assert failed_events[0]["payload"]["attempt"] == 1
    assert failed_events[0]["payload"]["will_retry"] is True
    assert failed_events[0]["payload"]["next_retry_at"] is not None
    assert failed_events[0]["payload"]["requires_human_alert"] is False

    # requested 事件应该已发出
    assert len(s.events.query("kill_switch/requested")) == 1
    # 没有 succeeded
    assert len(s.events.query("kill_switch/succeeded")) == 0

    # 失败记忆
    failed_mem = s.memory.recent(kind="kill-switch-failed")
    assert any("attempt 1/3" in m["content"] for m in failed_mem)


def test_kill_switch_retry_succeeds_on_second_attempt():
    """退避重试在第二次尝试时成功。"""
    call_count = [0]
    def kill(m, a):
        call_count[0] += 1
        if call_count[0] == 1:
            raise RuntimeError("transient")
        # 第二次成功

    v = _make_critical_violation()
    center = IncidentCenter(
        gateway=_gateway_stub(),
        risk_policy=_risk_policy_stub([v]),
        kill_switch=kill,
    )
    s = _session()
    center.tick(s)

    # 模拟重试到期：手动把 next_retry_at 改到过去
    from dsh_runtime.store import _get
    _get().execute(
        "UPDATE kill_switch_attempts SET next_retry_at = ? "
        "WHERE status = 'RETRYING'",
        ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(),),
    )
    _get().commit()

    # 再次 tick 应该重试并成功
    center.tick(s)

    assert call_count[0] == 2
    succeeded = s.events.query("kill_switch/succeeded")
    assert len(succeeded) == 1
    assert succeeded[0]["payload"]["attempt"] == 2


def test_kill_switch_max_attempts_emits_human_alert():
    """达到重试上限产生 requires_human_alert=True。"""
    def always_failing(m, a):
        raise RuntimeError("gateway permanently down")

    v = _make_critical_violation()
    center = IncidentCenter(
        gateway=_gateway_stub(),
        risk_policy=_risk_policy_stub([v]),
        kill_switch=always_failing,
    )
    s = _session()

    # 模拟已重试2次的状态
    from dsh_runtime.store import _get
    incident_id = "inc-test-1"
    # 先触发一次让 incident 创建
    center.tick(s)
    # 把 attempt_no 改为 2（模拟已重试1次）
    _get().execute(
        "UPDATE kill_switch_attempts SET attempt_no = ?, status = 'RETRYING' "
        "WHERE incident_id IN (SELECT incident_id FROM kill_switch_attempts)",
        (2,),
    )
    _get().commit()
    # 把 next_retry_at 设到过去
    _get().execute(
        "UPDATE kill_switch_attempts SET next_retry_at = ? "
        "WHERE status = 'RETRYING'",
        ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(),),
    )
    _get().commit()

    # 重试第3次（应该是 attempt_no=3，失败不再重试）
    center.tick(s)

    failed = s.events.query("kill_switch/failed")
    # events.query 返回倒序（最新在前），failed[0] 是最新的
    latest_failed = failed[0]
    assert latest_failed["payload"]["attempt"] == 3
    assert latest_failed["payload"]["will_retry"] is False
    assert latest_failed["payload"]["requires_human_alert"] is True


def test_kill_switch_failure_no_new_incident_opened():
    """Kill Switch 失败不产生新 incident/opened（避免重试循环）。"""
    def failing(m, a):
        raise RuntimeError("gateway down")

    center = IncidentCenter(
        gateway=_gateway_stub(),
        risk_policy=_risk_policy_stub([_make_critical_violation()]),
        kill_switch=failing,
    )
    s = _session()
    center.tick(s)

    incidents = s.events.query("incident/opened")
    # 只有最初的（如果有的话），没有 Kill Switch 失败产生的新 incident
    assert all("kill_switch_failed" not in i["payload"].get("reason", "")
               for i in incidents)
