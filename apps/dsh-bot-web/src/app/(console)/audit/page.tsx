import type { AuditReportRecord, TradeQualityReport } from "@dsh-bot/client-sdk";
import { requirePageViewer } from "@/lib/page-auth";
import { projection } from "@/lib/projection";

export const dynamic = "force-dynamic";
export const revalidate = 0;
export const fetchCache = "force-no-store";

export default async function AuditPage() {
  await requirePageViewer();
  const [report, intelligenceAudits] = await Promise.all([
    projection.getTradeQuality().catch(() => null),
    projection.getAuditReports(undefined, "intelligence-daily", 10).catch(() => [] as AuditReportRecord[]),
  ]);

  return (
    <main style={{ padding: 24 }}>
      <h1>交易质量审计</h1>
      <p>只审计，不改正在运行的策略，不会下单。建议停在 SUGGESTION。</p>
      {report ? <QualityReport report={report} intelligenceAudits={intelligenceAudits} /> : (
        <p style={{ color: "red" }}>
          无法加载审计。控制面 Projection `:8004` 需要已加载 `/v1/trade-quality`。
        </p>
      )}
    </main>
  );
}

function QualityReport({
  report,
  intelligenceAudits,
}: {
  report: TradeQualityReport;
  intelligenceAudits: AuditReportRecord[];
}) {
  const dims = report.score.dimensions || {};
  return (
    <section data-testid="trade-quality">
      <h2 style={{ marginTop: 0 }}>
        今日评分 {report.score.overall ?? "—"}
      </h2>
      <p style={{ color: "#6b7280" }}>{report.disclaimer}</p>
      <dl style={{ display: "grid", gridTemplateColumns: "120px 1fr", gap: "6px 12px" }}>
        {Object.entries(dims).map(([name, dim]) => (
          <span key={name} style={{ display: "contents" }}>
            <dt style={{ color: "#6b7280" }}>{labelOf(name)}</dt>
            <dd style={{ margin: 0 }}>
              {dim.available ? dim.score : "不可评"} · {dim.note}
            </dd>
          </span>
        ))}
      </dl>
      <h2>最差 / 最好</h2>
      {report.worst.length === 0 && report.best.length === 0 ? (
        <p>还没有闭环成交或带后续价的建议，排不出最差/最好三笔。</p>
      ) : (
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16 }}>
          <TradeList title="最差" rows={report.worst} />
          <TradeList title="最好" rows={report.best} />
        </div>
      )}
      <h2>缺口</h2>
      <ul>
        {report.exit_notes.map((note) => (
          <li key={note}>{note}</li>
        ))}
        <li>
          覆盖：Crypto 成交 {flag(report.coverage.crypto_fills)} · A 股成交{" "}
          {flag(report.coverage.ashare_fills)} · 手续费 {flag(report.coverage.fees_slippage)} ·
          退出原因 {flag(report.coverage.exit_reasons)} · 权益曲线{" "}
          {flag(report.coverage.daily_equity_curve)} · MAE/MFE {flag(report.coverage.mae_mfe)}
        </li>
      </ul>
      <h2>优化建议（不可直接应用）</h2>
      <ul>
        {report.suggestions.map((item) => (
          <li key={item.suggestion_id} style={{ marginBottom: 10 }}>
            <strong>{item.title}</strong>
            <div>{item.reason}</div>
            <div style={{ color: "#6b7280", fontSize: 13 }}>
              {item.pipeline} · 可应用：否
            </div>
          </li>
        ))}
      </ul>
      <h2>Intelligence Audit</h2>
      {intelligenceAudits.length === 0 ? (
        <p>还没有主动情报层的日报。</p>
      ) : (
        <ul>
          {intelligenceAudits.map((row) => {
            const payload = row.payload as Record<string, unknown>;
            const counts = payload.counts as Record<string, unknown> | undefined;
            const score = payload.score as Record<string, unknown> | undefined;
            return (
              <li key={row.report_id} style={{ marginBottom: 10 }}>
                <strong>{row.bot}</strong>
                {" · "}
                {row.period_key}
                {" · "}
                情报 {String(counts?.intelligence_items ?? "—")}
                {" · "}
                Shadow {String(counts?.shadow_decisions ?? "—")}
                {" · "}
                命中率 {score?.intelligence_hit_rate == null ? "—" : `${Math.round(Number(score.intelligence_hit_rate) * 100)}%`}
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}

function TradeList({
  title,
  rows,
}: {
  title: string;
  rows: Array<Record<string, unknown>>;
}) {
  return (
    <div>
      <h3 style={{ marginTop: 0 }}>{title}</h3>
      {rows.length === 0 ? (
        <p>暂无</p>
      ) : (
        <ul>
          {rows.map((row) => (
            <li key={String(row.task_id)}>
              {String(row.market)} {String(row.symbol)} {String(row.action)} · pnl{" "}
              {String(row.simulated_pnl)} · {String(row.reason)}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function labelOf(name: string): string {
  return {
    signal: "信号",
    entry: "进场",
    exit: "退出",
    size: "仓位",
    execution: "执行",
    capital: "资金",
  }[name] || name;
}

function flag(value: boolean | undefined): string {
  return value ? "有" : "无";
}
