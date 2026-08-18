import { BotConsole } from "@/components/bot-console";
import { requirePageViewer } from "@/lib/page-auth";
import { projection } from "@/lib/projection";

export const dynamic = "force-dynamic";

export default async function Home() {
  await requirePageViewer();
  const overview = await projection.getBotsOverview().catch(() => null);

  return (
    <main style={{ padding: 24 }}>
      <h1>DSH Bot</h1>
      <p>统一只读控制台。三个 Bot 实体，六维状态。LIVE 不可选。</p>
      {overview ? (
        <BotConsole overview={overview} />
      ) : (
        <p style={{ color: "red" }}>无法加载 Bot 总览：projection-api 不可用。</p>
      )}
    </main>
  );
}
