import type { ChiefBriefing, ShadowDecision } from "@dsh-bot/client-sdk";
import { requirePageViewer } from "@/lib/page-auth";
import { projection } from "@/lib/projection";

export const dynamic = "force-dynamic";
export const revalidate = 0;
export const fetchCache = "force-no-store";

export default async function ShadowPage() {
  await requirePageViewer();
  const [decisions, briefing, today] = await Promise.all([
    projection.getShadowDecisions().catch(() => null),
    projection.getChiefBriefing().catch(() => null),
    projection.getToday().catch(() => null),
  ]);

  return (
    <main style={{ padding: 24 }}>
      <h1>Shadow 决策</h1>
      <p>仅模拟，不会下单。这里是账本，不是今日结论。</p>
      {today && (
        <p style={{ marginTop: 0 }}>
          {today.headline} · <a href="/">回今日看板</a>
        </p>
      )}
      {briefing?.as_of && (
        <>
          <h2>Chief 简报</h2>
          <BriefingCard briefing={briefing} />
        </>
      )}
      <h2>决策记录</h2>
      <DecisionTable decisions={decisions} />
    </main>
  );
}

function BriefingCard({ briefing }: { briefing: ChiefBriefing | null }) {
  if (!briefing) {
    return <p style={{ color: "red" }}>无法加载 Chief 简报。</p>;
  }
  const counts = briefing.counts || {};
  return (
    <section style={{ marginBottom: 24 }}>
      <p>
        截至 {briefing.as_of || "—"}：决策 {counts.total ?? 0}，建议执行{" "}
        {counts.execute ?? 0}，放弃 {counts.abandon ?? 0}。
      </p>
      {(briefing.focus || []).length === 0 ? (
        <p>今日没有买入/卖出建议。</p>
      ) : (
        <ul>
          {briefing.focus.slice(0, 5).map((row) => (
            <li key={`${row.task_id}-${row.signal_id}`}>
              {row.market} {row.symbol} {row.action}（强度 {row.strength ?? "—"}）
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function DecisionTable({ decisions }: { decisions: ShadowDecision[] | null }) {
  if (!decisions) {
    return (
      <p style={{ color: "red" }}>
        无法加载 Shadow 决策。当前投影进程没有 /v1/shadow-decisions，重启
        projection-api（:8004）后再刷新。
      </p>
    );
  }
  if (decisions.length === 0) {
    return <p>暂无 Shadow 决策。</p>;
  }
  return (
    <table style={{ borderCollapse: "collapse", width: "100%" }}>
      <thead>
        <tr>
          {[
            "市场",
            "标的",
            "建议",
            "数量/价格",
            "策略/强度",
            "仓位/损失",
            "原因",
            "复盘",
          ].map((h) => (
            <th key={h} style={th}>
              {h}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {decisions.map((row) => (
          <tr key={row.task_id}>
            <td style={td}>{row.market || "—"}</td>
            <td style={td}>{row.symbol || "—"}</td>
            <td style={td}>
              {row.action || "—"}
              {row.skip_reason ? ` / ${row.skip_reason}` : ""}
            </td>
            <td style={td}>
              {row.quantity || "—"} @ {row.suggested_price || "—"}
              {row.price_low ? ` [${row.price_low}–${row.price_high}]` : ""}
            </td>
            <td style={td}>
              {row.strategy_version || "—"} / {row.strength ?? "—"}
            </td>
            <td style={td}>
              {row.position_after || "—"} / {row.worst_case_loss || "—"}
            </td>
            <td style={td}>
              {row.why || "—"}
              {row.why_not ? `；不执行：${row.why_not}` : ""}
              <div style={{ color: "#6b7280", fontSize: 12 }}>
                {row.disclaimer || "仅模拟，不会下单"}
              </div>
            </td>
            <td style={td}>
              {row.outcome_price
                ? `${row.suggested_price || "—"} → ${row.outcome_price} / ${row.simulated_pnl || "0"}`
                : "待跟踪"}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

const th = {
  border: "1px solid #e5e7eb",
  padding: "6px 10px",
  textAlign: "left" as const,
  backgroundColor: "#f9fafb",
};
const td = { border: "1px solid #e5e7eb", padding: "6px 10px", fontSize: 13 };
