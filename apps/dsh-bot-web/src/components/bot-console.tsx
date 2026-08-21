import Link from "next/link";
import type { BotOverview, BotsOverview, TodayStory } from "@dsh-bot/client-sdk";
import { dataLooksHealthy } from "@/lib/console-view";

const CARD_HREF: Record<BotOverview["bot_id"], string> = {
  "market-chief": "/chat",
  crypto: "/crypto",
  "a-share": "/a-share",
};

const STORY_BY_BOT: Record<BotOverview["bot_id"], string | null> = {
  "market-chief": null,
  crypto: "CRYPTO",
  "a-share": "A_SHARE",
};

export function BotConsole({
  overview,
  stories = [],
}: {
  overview: BotsOverview;
  stories?: TodayStory[];
}) {
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
          <BotCard
            key={bot.bot_id}
            bot={bot}
            summary={
              bot.bot_id === "market-chief"
                ? "汇总两市结论，不能下单"
                : stories.find((story) => story.market === STORY_BY_BOT[bot.bot_id])?.title
            }
          />
        ))}
      </div>
    </section>
  );
}

export function BotCard({
  bot,
  summary,
}: {
  bot: BotOverview;
  summary?: string;
}) {
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
      {summary && (
        <p style={{ margin: "8px 0 0", fontSize: 14, lineHeight: 1.5 }}>{summary}</p>
      )}
      <DimensionGrid bot={bot} />
      <p style={{ margin: "12px 0 0", fontSize: 12, color: "#6b7280" }}>
        连接 {bot.connection} · 时钟 {bot.clock_skew_ms ?? "—"}ms · as_of {bot.as_of}
      </p>
      {(bot.source_system || bot.source_mode || bot.source_observed_at) && (
        <p
          data-testid={`bot-source-${bot.bot_id}`}
          style={{ margin: "4px 0 0", fontSize: 12, color: "#6b7280" }}
        >
          来源 {bot.source_system || "—"} · 源模式 {bot.source_mode || "—"} ·
          观察 {bot.source_observed_at || "—"}
          {typeof bot.snapshot_age_seconds === "number"
            ? ` · 已停 ${bot.snapshot_age_seconds}s`
            : ""}
        </p>
      )}
      {bot.data === "STALE" && bot.connection === "CONNECTED" && (
        <p
          data-testid={`bot-plane-${bot.bot_id}`}
          style={{ margin: "4px 0 0", fontSize: 13, color: "#b91c1c" }}
        >
          控制面正常、数据面 STALE
        </p>
      )}
      {bot.degraded && bot.detail && (
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
  const dataColor =
    bot.data === "FRESH"
      ? "#15803d"
      : bot.data === "MARKET_CLOSED"
        ? "#6b7280"
        : "#b91c1c";
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
