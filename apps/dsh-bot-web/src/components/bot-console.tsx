import Link from "next/link";
import type { BotOverview, BotsOverview } from "@dsh-bot/client-sdk";
import { dataLooksHealthy } from "@/lib/console-view";

const CARD_HREF: Record<BotOverview["bot_id"], string> = {
  "market-chief": "/chat",
  crypto: "/crypto",
  "a-share": "/a-share",
};

export function BotConsole({ overview }: { overview: BotsOverview }) {
  return (
    <section data-testid="bot-console">
      {overview.alerts.length > 0 && (
        <div
          data-testid="console-alerts"
          style={{
            marginBottom: 16,
            padding: 12,
            border: "1px solid #fecaca",
            backgroundColor: "#fef2f2",
            color: "#991b1b",
          }}
        >
          {overview.alerts.map((alert) => (
            <div key={alert}>{alert}</div>
          ))}
        </div>
      )}
      <div
        style={{
          display: "grid",
          gap: 16,
          gridTemplateColumns: "repeat(3, minmax(0, 1fr))",
        }}
      >
        {overview.bots.map((bot) => (
          <BotCard key={bot.bot_id} bot={bot} />
        ))}
      </div>
    </section>
  );
}

export function BotCard({ bot }: { bot: BotOverview }) {
  const stale = !dataLooksHealthy(bot.data);
  return (
    <Link
      href={CARD_HREF[bot.bot_id]}
      data-testid={`bot-card-${bot.bot_id}`}
      style={{
        display: "block",
        padding: 16,
        border: "1px solid #e5e7eb",
        borderRadius: 8,
        textDecoration: "none",
        color: "#111827",
        backgroundColor: stale || bot.risk === "HALTED" ? "#fef2f2" : "#ffffff",
      }}
    >
      <div style={{ display: "flex", justifyContent: "space-between", gap: 8 }}>
        <strong>{bot.label}</strong>
        {bot.read_only && (
          <span data-testid="chief-readonly" style={{ fontSize: 12, color: "#6b7280" }}>
            READ ONLY
          </span>
        )}
      </div>
      <DimensionGrid bot={bot} />
      <p style={{ margin: "12px 0 0", fontSize: 12, color: "#6b7280" }}>
        连接 {bot.connection} · 时钟 {bot.clock_skew_ms ?? "—"}ms · as_of {bot.as_of}
      </p>
      {bot.detail && (
        <p style={{ margin: "4px 0 0", fontSize: 12, color: "#6b7280" }}>
          降级原因：{bot.detail}
        </p>
      )}
      {!bot.read_only && (
        <p style={{ margin: "8px 0 0", fontSize: 13 }}>
          待审批 {bot.counts.pending_approvals} · 在途 {bot.counts.open_orders} ·
          UNKNOWN {bot.counts.unknown_orders} · 事故 {bot.counts.incidents}
        </p>
      )}
    </Link>
  );
}

function DimensionGrid({ bot }: { bot: BotOverview }) {
  const dataColor = bot.data === "FRESH" ? "#15803d" : "#b91c1c";
  const rows: Array<[string, string, string | undefined]> = [
    ["Runtime", bot.runtime, undefined],
    ["Mode", bot.read_only ? "READ ONLY" : bot.mode, bot.mode === "LIVE" ? "#b91c1c" : undefined],
    ["Data", bot.data, dataColor],
    ["Task", bot.task, undefined],
    ["Order", bot.order, bot.order === "UNKNOWN" ? "#b91c1c" : undefined],
    ["Risk", bot.risk, bot.risk === "HALTED" || bot.risk === "INCIDENT" ? "#b91c1c" : undefined],
  ];
  return (
    <dl
      data-testid={`bot-dims-${bot.bot_id}`}
      style={{
        display: "grid",
        gridTemplateColumns: "auto 1fr",
        gap: "4px 12px",
        margin: "12px 0 0",
        fontSize: 13,
      }}
    >
      {rows.map(([label, value, color]) => (
        <span key={label} style={{ display: "contents" }}>
          <dt style={{ color: "#6b7280" }}>{label}</dt>
          <dd
            data-testid={`dim-${bot.bot_id}-${label.toLowerCase()}`}
            style={{ margin: 0, color: color || "#111827", fontWeight: 600 }}
          >
            {value}
          </dd>
        </span>
      ))}
    </dl>
  );
}
