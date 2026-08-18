import { ChatPanel } from "@/components/chat-panel";
import { requirePageViewer } from "@/lib/page-auth";

export const dynamic = "force-dynamic";
export const revalidate = 0;
export const fetchCache = "force-no-store";

export default async function ChatPage() {
  await requirePageViewer();
  return (
    <main style={{ padding: 24 }}>
      <h1>Chief Chat</h1>
      <p>只读解释与查询。Chief 不能批准、风控或下单。</p>
      <ChatPanel />
    </main>
  );
}
