import { ChatPanel } from "@/components/chat-panel";

// 对话上下文不缓存，避免陈旧会话。
export const dynamic = "force-dynamic";

export default function ChatPage() {
  return (
    <main style={{ padding: 24 }}>
      <h1>Chief Chat</h1>
      <p>与 Market Chief 对话（后端聊天 API 尚未接入，当前为本地占位交互）。</p>
      <ChatPanel />
    </main>
  );
}
