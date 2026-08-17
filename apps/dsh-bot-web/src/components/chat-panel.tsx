"use client";

import { FormEvent, useRef, useState } from "react";

interface ChatMessage {
  role: "user" | "bot";
  text: string;
}

/** Chief 只读对话：解释/查询投影数据，不批准、不风控、不下单。 */
export function ChatPanel() {
  const [messages, setMessages] = useState<ChatMessage[]>([
    {
      role: "bot",
      text: "你好，我是 Market Chief。我只能解释任务、对账和事故，不能批准、下单或操作 Kill Switch。",
    },
  ]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    const text = input.trim();
    if (!text || busy) return;
    setInput("");
    setBusy(true);
    setMessages((prev) => [...prev, { role: "user", text }]);
    try {
      const res = await fetch("/api/chief/query", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question: text }),
      });
      const data = (await res.json()) as { text?: string };
      setMessages((prev) => [
        ...prev,
        { role: "bot", text: data.text || `查询失败（${res.status}）。` },
      ]);
    } catch {
      setMessages((prev) => [
        ...prev,
        { role: "bot", text: "无法连接 Chief 查询接口。" },
      ]);
    } finally {
      setBusy(false);
      inputRef.current?.focus();
    }
  }

  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        height: "60vh",
        border: "1px solid #e5e7eb",
        borderRadius: 8,
      }}
    >
      <div
        style={{
          flex: 1,
          overflowY: "auto",
          padding: 16,
          display: "flex",
          flexDirection: "column",
          gap: 12,
        }}
      >
        {messages.map((msg, i) => (
          <div
            key={i}
            style={{
              alignSelf: msg.role === "user" ? "flex-end" : "flex-start",
              maxWidth: "70%",
              padding: "8px 12px",
              borderRadius: 12,
              backgroundColor: msg.role === "user" ? "#2563eb" : "#f3f4f6",
              color: msg.role === "user" ? "#ffffff" : "#111827",
              whiteSpace: "pre-wrap",
            }}
          >
            <div style={{ fontSize: 12, opacity: 0.7, marginBottom: 4 }}>
              {msg.role === "user" ? "我" : "Market Chief"}
            </div>
            {msg.text}
          </div>
        ))}
      </div>
      <form
        onSubmit={(e) => void handleSubmit(e)}
        style={{
          display: "flex",
          gap: 8,
          padding: 12,
          borderTop: "1px solid #e5e7eb",
        }}
      >
        <input
          ref={inputRef}
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="询问任务、对账或事故…"
          style={{ flex: 1, padding: "8px 12px", borderRadius: 6, border: "1px solid #d1d5db" }}
        />
        <button
          type="submit"
          disabled={busy}
          style={{
            padding: "8px 16px",
            borderRadius: 6,
            border: "none",
            backgroundColor: "#2563eb",
            color: "#ffffff",
            cursor: "pointer",
          }}
        >
          发送
        </button>
      </form>
    </div>
  );
}
