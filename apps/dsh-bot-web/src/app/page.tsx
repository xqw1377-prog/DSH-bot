import { MarketOverview } from "@/components/market-overview";

// 市场状态必须实时获取，不能静态预渲染出陈旧数据。
export const dynamic = "force-dynamic";

export default function Home() {
  return (
    <main style={{ padding: 24 }}>
      <h1>DSH Bot</h1>
      <p>持续进化量化 Agent 平台</p>
      <MarketOverview />
    </main>
  );
}
