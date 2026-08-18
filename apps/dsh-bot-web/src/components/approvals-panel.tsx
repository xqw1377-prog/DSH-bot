"use client";

import { useEffect, useState } from "react";
import type { Approval } from "@dsh-bot/client-sdk";
import { projection } from "@/lib/projection";

const SUBJECT_TYPE_LABELS: Record<Approval["subject_type"], string> = {
  order: "下单",
  strategy_promotion: "策略晋级",
  risk_budget: "风险预算",
  control_action: "控制动作",
};

/** 审批列表走投影；决定走 BFF。控件能力来自服务端 Principal。 */
export function ApprovalsPanel({ canDecide }: { canDecide: boolean }) {
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
    if (!canDecide) return;
    setDecidingId(id);
    setError(null);
    try {
      const csrf = await fetch("/api/csrf").then((r) => r.json()) as {
        csrf_token?: string;
      };
      const res = await fetch(`/api/approvals/${id}/decide`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": csrf.csrf_token || "",
        },
        body: JSON.stringify({ decision }),
      });
      if (!res.ok) {
        setError(`决定提交失败：BFF 返回 ${res.status}。`);
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
      {!canDecide && (
        <p data-testid="approver-required" style={{ color: "#6b7280" }}>
          需要 Approver 才能批准或拒绝。当前 Viewer 只能查看。
        </p>
      )}
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
                  type="button"
                  disabled={!canDecide || decidingId !== null}
                  data-testid="approve-button"
                  onClick={() => void decide(a.approval_id, "APPROVED")}
                  style={buttonStyle("#16a34a", !canDecide)}
                >
                  批准
                </button>
                <button
                  type="button"
                  disabled={!canDecide || decidingId !== null}
                  data-testid="reject-button"
                  onClick={() => void decide(a.approval_id, "REJECTED")}
                  style={buttonStyle("#dc2626", !canDecide)}
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

function buttonStyle(color: string, disabled = false) {
  return {
    padding: "6px 16px",
    borderRadius: 6,
    border: "none",
    backgroundColor: disabled ? "#9ca3af" : color,
    color: "#ffffff",
    cursor: disabled ? "not-allowed" : "pointer",
  };
}
