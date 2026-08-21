import type { AuditReportRecord, IntelligenceItem, IntelligenceReport } from "@dsh-bot/client-sdk";
import { requirePageViewer } from "@/lib/page-auth";
import { projection } from "@/lib/projection";

export const dynamic = "force-dynamic";
export const revalidate = 0;
export const fetchCache = "force-no-store";

export default async function IntelPage() {
  await requirePageViewer();
  const [report, feed, reports] = await Promise.all([
    projection.getIntelligence().catch(() => null),
    projection.getIntelligenceFeed(undefined, undefined, 20).catch(() => [] as IntelligenceItem[]),
    projection.getAuditReports(undefined, "intelligence-daily", 6).catch(() => [] as AuditReportRecord[]),
  ]);

  return (
    <main style={{ padding: 24 }}>
      <h1>事件情报</h1>
      <p>
        官方 API / RSS / 增量 HTML。事件只进 Shadow，不能直接下单。X
        必须走 Filtered Stream，禁止浏览器硬爬。
      </p>
      {report ? <IntelReport report={report} feed={feed} reports={reports} /> : (
        <p style={{ color: "red" }}>
          无法加载情报。需要 Projection `:8004` 已加载 `/v1/intelligence`，并且主动智能层至少跑过一轮。
        </p>
      )}
    </main>
  );
}

function IntelReport({
  report,
  feed,
  reports,
}: {
  report: IntelligenceReport;
  feed: IntelligenceItem[];
  reports: AuditReportRecord[];
}) {
  return (
    <section data-testid="intelligence">
      <p style={{ color: "#6b7280" }}>{report.disclaimer}</p>
      <p>
        模式 {report.mode} · 原文 {report.documents.length} · 事件 {report.events.length} · 结构化情报 {feed.length}
      </p>
      <h2>今天最该看</h2>
      {feed.length === 0 ? (
        <p>还没有结构化情报项。先跑自治层或执行一次 ingest。</p>
      ) : (
        <ul>
          {feed.slice(0, 5).map((row) => (
            <li key={row.item_id} style={{ marginBottom: 10 }}>
              <strong>{row.symbol || row.market}</strong>
              {" · "}
              {row.action || "WATCH"}
              {" · "}
              {Math.round(Number(row.confidence || 0) * 100)}%
              {" · "}
              {row.title}
              <div style={{ color: "#6b7280", fontSize: 13 }}>
                {row.source_id} · {row.direction || "NEUTRAL"} · {row.horizon || "medium"}
              </div>
            </li>
          ))}
        </ul>
      )}
      <h2>事件</h2>
      {report.events.length === 0 ? (
        <p>还没有可评分事件。先跑 `python -m intelligence_ingest ingest`，不要开万能爬虫。</p>
      ) : (
        <ul>
          {report.events.map((row) => (
            <li key={String(row.event_id)} style={{ marginBottom: 10 }}>
              <strong>{String(row.event_type || row.title || row.event_id)}</strong>
              {" · "}
              {String(row.direction || "UNCERTAIN")}
              {" · "}
              {String((row.affected_assets as string[] | undefined)?.join(", ") || "未映射持仓")}
              <div style={{ color: "#6b7280", fontSize: 13 }}>
                {String(row.canonical_url || "")} · 可应用：否
              </div>
            </li>
          ))}
        </ul>
      )}
      <h2>每日审计</h2>
      {reports.length === 0 ? (
        <p>还没有 Intelligence Audit 报告。</p>
      ) : (
        <ul>
          {reports.map((row) => {
            const score = (row.payload.score as Record<string, unknown> | undefined)?.intelligence_hit_rate;
            return (
              <li key={row.report_id} style={{ marginBottom: 10 }}>
                <strong>{row.bot}</strong>
                {" · "}
                {row.period_key}
                {" · "}
                命中率 {score == null ? "—" : `${Math.round(Number(score) * 100)}%`}
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
