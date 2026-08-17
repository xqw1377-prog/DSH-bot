"use client";

import { useCallback, useEffect, useState } from "react";
import type { Approval } from "@dsh-bot/client-sdk";
import { approvalActions, projection } from "@/lib/projection";

const SUBJECT_TYPE_LABELS: Record<Approval["subject_type"], string> = {
  order: "下单",
  strategy_promotion: "策略晋级",
  risk_budget: "风险预算",
  control_action: "控制动作",
};

/** 审批列表与批准/拒绝操作。通过 SDK 提交决定，不裸调网关。 */
export function ApprovalsPanel() {
  const [approvals, setApprovals] = useState<Approval[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [decidingId, setDecidingId] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      setApprovals(await projection.getApprovals("REQUESTED"));
    } catch {
      setApprovals([]);
      setError("无法加载待审批项：projection-api 不可用或返回错误。");
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function decide(id: string, decision: "APPROVED" | "REJECTED") {
    setDecidingId(id);
    setError(null);
    try {
      await approvalActions.decide(id, decision, "human");
    } catch (e) {
      setError(
        e instanceof Error
          ? `决定提交失败：${e.message}（可能 Quant Gateway 未启动）。`
          : "决定提交失败：无法连接 Quant Gateway。"
      );
    } finally {
      setDecidingId(null);
      await load();
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
