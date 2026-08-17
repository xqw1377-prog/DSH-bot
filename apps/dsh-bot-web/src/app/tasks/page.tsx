import { projection } from "@/lib/projection";

export const dynamic = "force-dynamic";

export default async function TasksPage() {
  const tasks = await projection.getBotTasks("crypto-bot").catch(() => null);

  return (
    <main style={{ padding: 24 }}>
      <h1>任务与对账</h1>
      <p>Crypto Bot 任务终态与 reconciliation_status。人工处理 INCIDENT / MISMATCH。</p>
      {tasks === null ? (
        <p style={{ color: "red" }}>无法加载任务：projection-api 或 Runtime DB 不可用。</p>
      ) : tasks.length === 0 ? (
        <p>暂无任务。</p>
      ) : (
        <table style={{ borderCollapse: "collapse", width: "100%" }}>
          <thead>
            <tr>
              {["任务", "状态", "对账", "订单", "更新"].map((h) => (
                <th
                  key={h}
                  style={{
                    border: "1px solid #e5e7eb",
                    padding: "6px 10px",
                    textAlign: "left",
                    backgroundColor: "#f9fafb",
                  }}
                >
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {tasks.map((t) => (
              <tr key={t.task_id}>
                <td style={td}>{t.task_id}</td>
                <td style={td}>{t.status}</td>
                <td style={td}>{t.reconciliation_status}</td>
                <td style={td}>{t.order_id || "—"}</td>
                <td style={td}>{t.updated_at}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </main>
  );
}

const td = { border: "1px solid #e5e7eb", padding: "6px 10px", fontSize: 14 };
