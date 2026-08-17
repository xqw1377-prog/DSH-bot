import Link from "next/link";
import { MarketOverview } from "@/components/market-overview";

// 市场状态必须实时获取，不能静态预渲染出陈旧数据。
export const dynamic = "force-dynamic";

const ENTRIES = [
  { href: "/chat", label: "Chief Chat", desc: "与 Market Chief 对话" },
  { href: "/approvals", label: "审批中心", desc: "处理待审批的资金与晋级动作" },
  { href: "/tasks", label: "任务与对账", desc: "订单任务、MATCHED / INCIDENT" },
  { href: "/portfolio", label: "Portfolio", desc: "持仓与账户摘要" },
  { href: "/strategy-lab", label: "Strategy Lab", desc: "实验与策略晋级状态" },
];

export default function Home() {
  return (
    <main style={{ padding: 24 }}>
      <h1>DSH Bot</h1>
      <p>持续进化量化 Agent 平台</p>
      <MarketOverview />
      <section style={{ marginTop: 24 }}>
        <h2>功能入口</h2>
        <div style={{ display: "grid", gap: 16, gridTemplateColumns: "1fr 1fr" }}>
          {ENTRIES.map((entry) => (
            <Link
              key={entry.href}
              href={entry.href}
              style={{
                padding: 16,
                border: "1px solid #e5e7eb",
                borderRadius: 8,
                textDecoration: "none",
                color: "#111827",
                display: "block",
              }}
            >
              <div style={{ fontWeight: 600 }}>{entry.label}</div>
              <div style={{ color: "#6b7280", fontSize: 14 }}>{entry.desc}</div>
            </Link>
          ))}
        </div>
      </section>
    </main>
  );
}
