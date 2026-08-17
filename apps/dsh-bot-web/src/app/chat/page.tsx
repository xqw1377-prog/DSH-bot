import { ChatPanel } from "@/components/chat-panel";

// 对话上下文不缓存，避免陈旧会话。
export const dynamic = "force-dynamic";

export default function ChatPage() {
  return (
    <main style={{ padding: 24 }}>
      <h1>Chief Chat</h1>
      <p>只读解释与查询。Chief 不能批准、风控或下单。</p>
      <ChatPanel />
    </main>
  );
}
