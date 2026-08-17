"use client";

import { useEffect, useState } from "react";
import type { Approval } from "@dsh-bot/client-sdk";
import { projection } from "@/lib/projection";

// 决定动作不走 projection-api（只读），直接提交 Quant Gateway。
const GATEWAY_URL =
  process.env.NEXT_PUBLIC_QUANT_GATEWAY_URL || "http://127.0.0.1:8001";

const SUBJECT_TYPE_LABELS: Record<Approval["subject_type"], string> = {
  order: "下单",
  strategy_promotion: "策略晋级",
  risk_budget: "风险预算",
  control_action: "控制动作",
};

/** 审批列表与批准/拒绝操作。决定动作直接 POST 到 Quant Gateway（projection-api 只读）。 */
export function ApprovalsPanel() {
  const [approvals, setApprovals] = useState<Approval[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [decidingId, setDecidingId] = useState<string | null>(null);

  // 挂载后异步加载审批列表；渲染期不产生级联 setState
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const items = await projection.getApprovals("REQUESTED");
        if (!cancelled) {
          setApprovals(items);
          setError(null);
        }
      } catch {
        if (!cancelled) {
          setApprovals([]);
          setError("无法加载待审批项：projection-api 不可用或返回错误。");
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  async function decide(id: string, decision: "APPROVED" | "REJECTED") {
    setDecidingId(id);
    setError(null);
    try {
      const res = await fetch(`${GATEWAY_URL}/v1/approvals/${id}/decide`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ decision, decided_by: "human" }),
      });
      if (!res.ok) {
        setError(`决定提交失败：Quant Gateway 返回 ${res.status}。`);
      }
    } catch {
      setError("决定提交失败：无法连接 Quant Gateway（可能未启动）。");
    } finally {
      setDecidingId(null);
      // 决定后重新拉取列表（异步回调内 setState 合法）
      try {
        setApprovals(await projection.getApprovals("REQUESTED"));
      } catch {
        setApprovals([]);
      }
    }
  }

  return (
    <section>
      {error && <p style={{ color: "red" }}>{error}</p>}
      {approvals === null ? (
        <p>加载中…</p>
      ) : approvals.length === 0 && !error ? (
        <p>暂无待审批项。</p>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          {approvals.map((a) => (
            <div
              key={a.approval_id}
              style={{
                padding: 16,
                border: "1px solid #e5e7eb",
                borderRadius: 8,
              }}
            >
              <div style={{ display: "flex", justifyContent: "space-between" }}>
                <strong>{SUBJECT_TYPE_LABELS[a.subject_type]}</strong>
                <span style={{ color: "#6b7280", fontSize: 13 }}>
                  {a.market} · 请求于 {a.requested_at}
                </span>
              </div>
              <p style={{ margin: "8px 0", fontSize: 14 }}>
                主体：{a.subject_id} · 发起 Bot：{a.requested_by_bot}
              </p>
              {a.evidence_refs.length > 0 && (
                <p style={{ margin: "4px 0 12px", fontSize: 13, color: "#6b7280" }}>
                  证据引用：{a.evidence_refs.join("、")}
                </p>
              )}
              <div style={{ display: "flex", gap: 8 }}>
                <button
                  disabled={decidingId !== null}
                  onClick={() => void decide(a.approval_id, "APPROVED")}
                  style={buttonStyle("#16a34a")}
                >
                  批准
                </button>
                <button
                  disabled={decidingId !== null}
                  onClick={() => void decide(a.approval_id, "REJECTED")}
                  style={buttonStyle("#dc2626")}
                >
                  拒绝
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

function buttonStyle(color: string) {
  return {
    padding: "6px 16px",
    borderRadius: 6,
    border: "none",
    backgroundColor: color,
    color: "#ffffff",
    cursor: "pointer",
  };
}
