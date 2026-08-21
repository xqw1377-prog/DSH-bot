import type { BotTask, Market, ShadowDecision, Signal } from "@dsh-bot/client-sdk";
import { projection } from "@/lib/projection";

export async function MarketDrilldown({
  market,
  title,
  bot,
}: {
  market: Market;
  title: string;
  bot: string;
}) {
  const [signals, tasks, decisions] = await Promise.all([
    projection.getSignals(market).catch(() => null),
    projection.getBotTasks(bot).catch(() => null),
    projection.getShadowDecisions(bot).catch(() => null),
  ]);
  const orders = (tasks || []).filter((task) => task.order_id);

  return (
    <main style={{ padding: 24 }}>
      <h1>{title}</h1>
      <p>只读。先看策略现在想做什么，再看正式信号。不会下单。</p>
      <h2>现在想做什么</h2>
      <DecisionList decisions={decisions} />
      <h2>正式信号</h2>
      <SignalsTable signals={signals} />
      <h2>任务</h2>
      <OrdersTable tasks={orders.length ? orders : tasks} />
    </main>
  );
}

function DecisionList({ decisions }: { decisions: ShadowDecision[] | null }) {
  if (!decisions) {
    return (
      <p style={{ color: "red" }}>
        无法加载 Shadow 决策。控制面 Projection `:8004` 不可达，不是策略没信号。
      </p>
    );
  }
  if (decisions.length === 0) {
    return <p>还没有 Shadow 决策。需要正式信号，并且 Bot 已按 shadow 跑过一轮。</p>;
  }
  return (
    <ul>
      {decisions.slice(0, 8).map((row) => (
        <li key={row.task_id} style={{ marginBottom: 8 }}>
          <strong>
            {row.action}
            {row.skip_reason ? ` / ${row.skip_reason}` : ""}
          </strong>{" "}
          {row.symbol} {row.quantity} @ {row.suggested_price || "—"}
          <div style={{ color: "#4b5563", fontSize: 13 }}>
            {row.why}
            {row.why_not ? ` ${row.why_not}` : ""}
            {row.outcome_price
              ? ` 复盘 ${row.suggested_price} → ${row.outcome_price}（${row.simulated_pnl}）`
              : ""}
          </div>
        </li>
      ))}
    </ul>
  );
}

function SignalsTable({ signals }: { signals: Signal[] | null }) {
  if (!signals) {
    return (
      <p style={{ color: "red" }}>
        无法加载 signals。控制面不可达（Gateway/Projection），数据面导出循环仍可能在写。
      </p>
    );
  }
  if (signals.length === 0) {
    return <p>暂无信号。</p>;
  }
  return (
    <table style={{ borderCollapse: "collapse", width: "100%" }}>
      <thead>
        <tr>
          {["信号", "标的", "方向", "策略", "生成"].map((h) => (
            <th key={h} style={th}>
              {h}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {signals.map((s) => (
          <tr key={s.signal_id}>
            <td style={td}>{s.signal_id}</td>
            <td style={td}>{s.symbol}</td>
            <td style={td}>{s.side}</td>
            <td style={td}>
              {s.strategy_id} {s.strategy_version}
            </td>
            <td style={td}>{s.generated_at}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function OrdersTable({ tasks }: { tasks: BotTask[] | null }) {
  if (!tasks) {
    return (
      <p style={{ color: "red" }}>
        无法加载订单任务。控制面 Projection `:8004` 不可达。
      </p>
    );
  }
  if (tasks.length === 0) {
    return <p>暂无订单任务。</p>;
  }
  return (
    <table style={{ borderCollapse: "collapse", width: "100%", marginTop: 8 }}>
      <thead>
        <tr>
          {["任务", "状态", "对账", "订单", "更新"].map((h) => (
            <th key={h} style={th}>
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
  );
}

const th = {
  border: "1px solid #e5e7eb",
  padding: "6px 10px",
  textAlign: "left" as const,
  backgroundColor: "#f9fafb",
};
const td = { border: "1px solid #e5e7eb", padding: "6px 10px", fontSize: 14 };
