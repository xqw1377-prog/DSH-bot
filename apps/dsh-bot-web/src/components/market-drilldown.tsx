import type { BotTask, Market, Signal } from "@dsh-bot/client-sdk";
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
  const [signals, tasks] = await Promise.all([
    projection.getSignals(market).catch(() => null),
    projection.getBotTasks(bot).catch(() => null),
  ]);
  const orders = (tasks || []).filter((task) => task.order_id);

  return (
    <main style={{ padding: 24 }}>
      <h1>{title}</h1>
      <p>Signals 与 Orders 下钻。只读，不改资金路径。</p>
      <h2>Signals</h2>
      <SignalsTable signals={signals} />
      <h2>Orders / 任务</h2>
      <OrdersTable tasks={orders.length ? orders : tasks} />
    </main>
  );
}

function SignalsTable({ signals }: { signals: Signal[] | null }) {
  if (!signals) {
    return <p style={{ color: "red" }}>无法加载 signals。</p>;
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
    return <p style={{ color: "red" }}>无法加载订单任务。</p>;
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
