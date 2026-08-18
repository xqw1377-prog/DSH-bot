import type { Experiment, StrategyCandidate, StrategyStage } from "@dsh-bot/client-sdk";
import { requirePageViewer } from "@/lib/page-auth";
import { projection } from "@/lib/projection";

// 实验与候选状态必须实时获取。
export const dynamic = "force-dynamic";

/** 晋级主链：stage 推进必须附带证据引用，见 PRD 附录 A.1。 */
const MAIN_CHAIN: StrategyStage[] = [
  "DRAFT",
  "BACKTESTED",
  "VALIDATED",
  "PAPER",
  "SHADOW",
  "APPROVED",
  "CANARY",
  "PRODUCTION",
];

const STAGE_LABELS: Record<StrategyStage, string> = {
  DRAFT: "草稿",
  BACKTESTED: "已回测",
  VALIDATED: "已验证",
  PAPER: "纸上交易",
  SHADOW: "影子运行",
  APPROVED: "已批准",
  CANARY: "金丝雀",
  PRODUCTION: "生产",
  RETIRED: "已退役",
  ROLLED_BACK: "已回滚",
};

const TASK_STATUS_LABELS: Record<Experiment["status"], string> = {
  QUEUED: "排队中",
  RUNNING: "运行中",
  WAITING_FOR_TOOL: "等待工具",
  WAITING_FOR_APPROVAL: "等待审批",
  COMPLETED: "已完成",
  FAILED: "失败",
  CANCELLED: "已取消",
};

export default async function StrategyLabPage() {
  await requirePageViewer();
  const [experiments, candidates] = await Promise.all([
    projection.getExperiments().catch(() => null),
    projection.getCandidates().catch(() => null),
  ]);

  return (
    <main style={{ padding: 24 }}>
      <h1>Strategy Lab</h1>

      <h2>晋级链</h2>
      <StageChain />

      <h2>策略候选</h2>
      <CandidatesList candidates={candidates} />

      <h2>实验</h2>
      <ExperimentsList experiments={experiments} />
    </main>
  );
}

/** 主链可视化：DRAFT → … → PRODUCTION。 */
function StageChain() {
  return (
    <div style={{ display: "flex", flexWrap: "wrap", gap: 4, alignItems: "center" }}>
      {MAIN_CHAIN.map((stage, i) => (
        <span key={stage} style={{ display: "flex", alignItems: "center", gap: 4 }}>
          {i > 0 && <span style={{ color: "#9ca3af" }}>→</span>}
          <span
            style={{
              padding: "2px 10px",
              borderRadius: 999,
              fontSize: 13,
              border: "1px solid #d1d5db",
              color: "#374151",
              backgroundColor: "#f9fafb",
            }}
          >
            {STAGE_LABELS[stage]}
          </span>
        </span>
      ))}
    </div>
  );
}

function CandidatesList({ candidates }: { candidates: StrategyCandidate[] | null }) {
  if (!candidates) {
    return <p style={{ color: "red" }}>无法加载策略候选：projection-api 不可用。</p>;
  }
  if (candidates.length === 0) {
    return <p>暂无策略候选。</p>;
  }
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      {candidates.map((c) => (
        <div key={c.candidate_id} style={{ padding: 16, border: "1px solid #e5e7eb", borderRadius: 8 }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <strong>
              {c.strategy_id} · {c.strategy_version}
            </strong>
            <StageBadge stage={c.stage} />
          </div>
          <p style={{ margin: "8px 0 4px", fontSize: 14, color: "#6b7280" }}>
            市场：{c.market} · 更新于 {c.updated_at}
          </p>
          {c.experiment_id && (
            <p style={{ margin: "4px 0", fontSize: 13, color: "#6b7280" }}>实验：{c.experiment_id}</p>
          )}
          {c.evidence_refs.length > 0 && (
            <p style={{ margin: "4px 0", fontSize: 13, color: "#6b7280" }}>
              证据引用：{c.evidence_refs.join("、")}
            </p>
          )}
        </div>
      ))}
    </div>
  );
}

function StageBadge({ stage }: { stage: StrategyStage }) {
  const terminal = stage === "RETIRED" || stage === "ROLLED_BACK";
  return (
    <span
      style={{
        padding: "2px 10px",
        borderRadius: 999,
        fontSize: 13,
        color: terminal ? "#ffffff" : "#111827",
        backgroundColor: terminal ? "#dc2626" : "#e5e7eb",
        fontWeight: terminal ? 600 : 400,
      }}
    >
      {STAGE_LABELS[stage]}
    </span>
  );
}

function ExperimentsList({ experiments }: { experiments: Experiment[] | null }) {
  if (!experiments) {
    return <p style={{ color: "red" }}>无法加载实验：projection-api 不可用。</p>;
  }
  if (experiments.length === 0) {
    return <p>暂无实验。</p>;
  }
  return (
    <table style={{ borderCollapse: "collapse", width: "100%" }}>
      <thead>
        <tr>
          {["实验", "市场", "策略", "假设", "状态", "发起 Bot", "创建时间"].map((h) => (
            <th key={h} style={{ border: "1px solid #e5e7eb", padding: "6px 10px", textAlign: "left", fontSize: 14, backgroundColor: "#f9fafb" }}>
              {h}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {experiments.map((e) => (
          <tr key={e.experiment_id}>
            <td style={td}>{e.experiment_id}</td>
            <td style={td}>{e.market}</td>
            <td style={td}>{e.strategy_id}</td>
            <td style={td}>{e.hypothesis}</td>
            <td style={td}>{TASK_STATUS_LABELS[e.status]}</td>
            <td style={td}>{e.created_by_bot}</td>
            <td style={td}>{e.created_at}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

const td = { border: "1px solid #e5e7eb", padding: "6px 10px", fontSize: 14 };
