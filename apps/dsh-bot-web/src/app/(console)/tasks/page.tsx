import type { BotTask } from "@dsh-bot/client-sdk";
import { requirePageViewer } from "@/lib/page-auth";
import { projection } from "@/lib/projection";

export const dynamic = "force-dynamic";
export const revalidate = 0;
export const fetchCache = "force-no-store";

export default async function TasksPage() {
  await requirePageViewer();
  const tasks = await projection.getBotTasks().catch(() => null);

  return (
    <main style={{ padding: 24 }}>
      <h1>任务与对账</h1>
      <p>
        成功终态 DONE + MATCHED。INCIDENT / MISMATCH / UNKNOWN 需人工核查，
        系统不会重下。
      </p>
      {tasks === null ? (
        <p style={{ color: "red" }}>无法加载任务：projection-api 或 Runtime DB 不可用。</p>
      ) : tasks.length === 0 ? (
        <p>暂无任务。</p>
      ) : (
        <table style={{ borderCollapse: "collapse", width: "100%" }}>
          <thead>
            <tr>
              {["Bot", "任务", "状态", "对账", "订单", "原因", "更新"].map((h) => (
                <th key={h} style={th}>
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {tasks.map((t: BotTask) => {
              const decision = (t.payload && (t.payload as { shadow_decision?: Record<string, unknown> }).shadow_decision) || {};
              const reason = String(
                decision.skip_reason
                || decision.action
                || (t.payload && (t.payload.reason || t.payload.unknown_since))
                || "—",
              );
              const hot = t.status === "INCIDENT" || t.reconciliation_status === "MISMATCH";
              return (
                <tr key={t.task_id} style={hot ? { backgroundColor: "#fef2f2" } : undefined}>
                  <td style={td}>{t.bot}</td>
                  <td style={td}>{t.task_id}</td>
                  <td style={td}>{t.status}</td>
                  <td style={td}>{t.reconciliation_status}</td>
                  <td style={td}>{t.order_id || "—"}</td>
                  <td style={td}>{reason}</td>
                  <td style={td}>{t.updated_at}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </main>
  );
}

const th = {
  border: "1px solid #e5e7eb",
  padding: "6px 10px",
  textAlign: "left" as const,
  backgroundColor: "#f9fafb",
};
const td = { border: "1px solid #e5e7eb", padding: "6px 10px", fontSize: 14 };
