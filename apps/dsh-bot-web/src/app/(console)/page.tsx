import { BotConsole } from "@/components/bot-console";
import { TodayBoardView } from "@/components/today-board";
import { requirePageViewer } from "@/lib/page-auth";
import { projection } from "@/lib/projection";

export const dynamic = "force-dynamic";
export const revalidate = 0;
export const fetchCache = "force-no-store";

export default async function Home() {
  await requirePageViewer();
  const [overview, today, quality] = await Promise.all([
    projection.getBotsOverview().catch(() => null),
    projection.getToday().catch(() => null),
    projection.getTradeQuality().catch(() => null),
  ]);

  return (
    <main style={{ padding: 24 }}>
      <h1>DSH Bot</h1>
      <p>本机双市场只读助手。先看今天结论，再看三个 Bot 状态。LIVE 不可选。</p>
      {quality?.score.overall != null && (
        <p>
          交易质量评分 {quality.score.overall} · 建议 {quality.suggestions.length} 条，均不可直接应用。{" "}
          <a href="/audit">看审计</a>
        </p>
      )}
      {today ? (
        <TodayBoardView today={today} />
      ) : (
        <p style={{ color: "#92400e" }}>
          作战板接口未加载（/v1/today）。总览能开说明投影还活着，但进程是旧代码。重启
          projection-api（:8004）后再刷新，不必重导快照。
        </p>
      )}
      {overview ? (
        <BotConsole overview={overview} stories={today?.stories || []} />
      ) : (
        <p style={{ color: "red" }}>无法加载 Bot 总览：projection-api 不可用。</p>
      )}
    </main>
  );
}
