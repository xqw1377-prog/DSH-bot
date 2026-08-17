"""多 worker 幂等并发测试（真实多进程）。

启动 2 个 uvicorn worker（独立进程，共享同一 SQLite 文件库），
8 个并发客户端进程用同一幂等键提交相同订单：
- 恰好一个 200，其余 409
- 网关审计中 order.submitted 恰好一条（venue 只收到一笔）
"""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent.parent.parent

SUBMIT_SCRIPT = """
import json, sys
import httpx
body = json.loads(sys.argv[2])
try:
    r = httpx.post(sys.argv[1] + "/v1/markets/CRYPTO/orders", json=body, timeout=30)
    print(r.status_code)
except Exception as e:
    print("ERR", e)
"""


@pytest.fixture()
def multiworker_gateway(tmp_path):
    db = tmp_path / "gw.db"
    env = dict(
        os.environ,
        QUANT_GATEWAY_DB=str(db),
        RISK_POLICY_URL="http://127.0.0.1:8093",
        DSH_ENV="development",
        DSH_LOCAL_PAPER="1",
        PAPER_CRYPTO_ACCOUNT_ID="paper-crypto-001",
    )
    gw = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "quant_gateway.main:app",
         "--port", "8091", "--workers", "2"],
        env=env, cwd=str(ROOT), stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base = "http://127.0.0.1:8091"
    for _ in range(60):
        try:
            if httpx.get(base + "/healthz", timeout=2).status_code == 200:
                break
        except Exception:
            time.sleep(0.5)
    else:
        gw.terminate()
        pytest.fail("gateway did not start")
    rp = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "risk_policy.main:app",
         "--port", "8093"],
        env=env, cwd=str(ROOT), stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    # 等 risk-policy 就绪（二次硬风控不可达时网关失败关闭，全是 503）
    for _ in range(60):
        try:
            httpx.get("http://127.0.0.1:8093/healthz", timeout=2)
            break
        except Exception:
            time.sleep(0.5)
    yield base
    gw.send_signal(signal.SIGTERM); gw.wait(timeout=10)
    rp.send_signal(signal.SIGTERM); rp.wait(timeout=10)


def test_concurrent_processes_submit_exactly_once(multiworker_gateway, tmp_path):
    base = multiworker_gateway

    # 准备：审批 + 风险快照（经 API，走完整门禁）
    approval = httpx.post(base + "/v1/approvals", json={
        "market": "CRYPTO", "requested_by_bot": "crypto-bot",
        "subject_type": "order", "subject_id": "sig-mw",
    }).json()
    httpx.post(
        base + f"/v1/approvals/{approval['approval_id']}/decide",
        json={"decision": "APPROVED", "decided_by": "alice"},
    )
    httpx.post(base + "/v1/markets/CRYPTO/risk-snapshots", json={
        "risk_snapshot_id": "rs-mw", "market": "CRYPTO",
        "account_id": "paper-crypto-001",
        "position_before": "0", "position_after": "0.01",
        "risk_budget_delta": "1", "worst_case_loss": "1",
        "limits_hit": [], "as_of": "2026-01-01T00:00:00Z",
    })
    body = {
        "idempotency_key": "mw-key-1",
        "market": "CRYPTO", "account_id": "paper-crypto-001",
        "strategy_id": "s", "strategy_version": "1",
        "symbol": "BTCUSDT", "side": "BUY", "quantity": "0.01",
        "valid_until": "2030-01-01T00:00:00Z",
        "signal_snapshot_id": "sig-mw", "risk_snapshot_id": "rs-mw",
        "approval_id": approval["approval_id"],
    }

    script = tmp_path / "submit.py"
    script.write_text(SUBMIT_SCRIPT)

    # 8 个并发进程，同一幂等键同一请求体
    procs = [
        subprocess.Popen(
            [sys.executable, str(script), base, json.dumps(body)],
            stdout=subprocess.PIPE, text=True,
        )
        for _ in range(8)
    ]
    results = [p.communicate()[0].strip() for p in procs]

    assert results.count("200") == 1, results
    assert results.count("409") == len(results) - 1, results

    audit = httpx.get(base + "/v1/audit?limit=50").json()
    submitted = [e for e in audit if e["action"] == "order.submitted"]
    assert len(submitted) == 1  # venue 只收到一笔
