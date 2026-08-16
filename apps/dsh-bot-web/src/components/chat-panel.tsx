"use client";

import { FormEvent, useRef, useState } from "react";

interface ChatMessage {
  role: "user" | "bot";
  text: string;
}

/** Chief Chat 对话面板。后端聊天 API 尚未接入：发送后仅展示占位回复。 */
export function ChatPanel() {
  const [messages, setMessages] = useState<ChatMessage[]>([
    {
      role: "bot",
      text: "你好，我是 Market Chief。后端聊天 API 尚未接入，当前回复为占位内容。",
    },
  ]);
  const [input, setInput] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);

  function handleSubmit(e: FormEvent) {
    e.preventDefault();
    const text = input.trim();
    if (!text) return;
    setMessages((prev) => [
      ...prev,
      { role: "user", text },
      {
        role: "bot",
        text: "（占位回复）后端聊天 API 尚未接入，无法处理该消息。接入后将由 Market Chief 生成真实回复。",
      },
    ]);
    setInput("");
    inputRef.current?.focus();
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
        onSubmit={handleSubmit}
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
          placeholder="输入消息…"
          style={{ flex: 1, padding: "8px 12px", borderRadius: 6, border: "1px solid #d1d5db" }}
        />
        <button
          type="submit"
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
