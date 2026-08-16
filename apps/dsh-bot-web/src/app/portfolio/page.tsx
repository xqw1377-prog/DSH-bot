import Link from "next/link";
import type { AccountSummary, Market, Position } from "@dsh-bot/client-sdk";
import { projection } from "@/lib/projection";

// 行情与持仓必须实时获取，不能静态预渲染出陈旧数据。
export const dynamic = "force-dynamic";

const MARKETS: { value: Market; label: string }[] = [
  { value: "A_SHARE", label: "A 股" },
  { value: "CRYPTO", label: "数字资产" },
];

export default async function PortfolioPage({
  searchParams,
}: {
  searchParams: Promise<{ market?: string }>;
}) {
  const params = await searchParams;
  const market: Market = params.market === "CRYPTO" ? "CRYPTO" : "A_SHARE";

  const [positions, accounts] = await Promise.all([
    projection.getPositions(market).catch(() => null),
    projection.getAccountSummary(market).catch(() => null),
  ]);

  return (
    <main style={{ padding: 24 }}>
      <h1>Portfolio</h1>
      <div style={{ display: "flex", gap: 8, marginBottom: 16 }}>
        {MARKETS.map((m) => (
          <Link
            key={m.value}
            href={`/portfolio?market=${m.value}`}
            style={{
              padding: "6px 16px",
              borderRadius: 6,
              border: "1px solid #d1d5db",
              textDecoration: "none",
              color: market === m.value ? "#ffffff" : "#111827",
              backgroundColor: market === m.value ? "#2563eb" : "#ffffff",
            }}
          >
            {m.label}
          </Link>
        ))}
      </div>

      <h2>账户摘要</h2>
      <AccountCards accounts={accounts} />

      <h2>持仓</h2>
      <PositionsTable positions={positions} />
    </main>
  );
}

function AccountCards({ accounts }: { accounts: AccountSummary[] | null }) {
  if (!accounts) {
    return <p style={{ color: "red" }}>无法加载账户摘要：projection-api 不可用。</p>;
  }
  if (accounts.length === 0) {
    return <p>暂无账户数据。</p>;
  }
  return (
    <div style={{ display: "grid", gap: 16, gridTemplateColumns: "1fr 1fr" }}>
      {accounts.map((a) => (
        <div key={a.account_id} style={{ padding: 16, border: "1px solid #e5e7eb", borderRadius: 8 }}>
          <strong>{a.account_id}</strong>
          <p style={{ margin: "8px 0 0" }}>
            现金：{a.cash} {a.currency} · 权益：{a.equity} {a.currency}
          </p>
          <p style={{ margin: "4px 0 0", fontSize: 13, color: "#6b7280" }}>
            对账版本：{a.reconciliation_version} · 截至 {a.as_of}
          </p>
        </div>
      ))}
    </div>
  );
}

function PositionsTable({ positions }: { positions: Position[] | null }) {
  if (!positions) {
    return <p style={{ color: "red" }}>无法加载持仓：projection-api 不可用。</p>;
  }
  if (positions.length === 0) {
    return <p>暂无持仓。</p>;
  }
  return (
    <table style={{ borderCollapse: "collapse", width: "100%" }}>
      <thead>
        <tr>
          {["标的", "账户", "数量", "可用", "冻结", "成本", "币种", "截至"].map((h) => (
            <th key={h} style={cellStyle({ header: true })}>
              {h}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {positions.map((p) => (
          <tr key={`${p.account_id}:${p.symbol}`}>
            <td style={cellStyle({})}>{p.symbol}</td>
            <td style={cellStyle({})}>{p.account_id}</td>
            <td style={cellStyle({})}>{p.quantity}</td>
            <td style={cellStyle({})}>{p.available_quantity}</td>
            <td style={cellStyle({})}>{p.frozen_quantity}</td>
            <td style={cellStyle({})}>{p.avg_cost}</td>
            <td style={cellStyle({})}>{p.currency}</td>
            <td style={cellStyle({})}>{p.as_of}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function cellStyle({ header }: { header?: boolean }) {
  return {
    border: "1px solid #e5e7eb",
    padding: "6px 10px",
    textAlign: "left" as const,
    fontSize: 14,
    backgroundColor: header ? "#f9fafb" : undefined,
  };
}
