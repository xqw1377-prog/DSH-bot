"use client";

import { useEffect, useState } from "react";
import type { IncidentEvent } from "@dsh-bot/client-sdk";
import { projection } from "@/lib/projection";
import { writeActionProps } from "@/lib/console-view";

export function IncidentsPanel({
  canEmergencyStop,
}: {
  canEmergencyStop: boolean;
}) {
  const [items, setItems] = useState<IncidentEvent[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [stopping, setStopping] = useState(false);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const next = await projection.getIncidents(50);
        if (!cancelled) {
          setItems(next);
          setError(null);
        }
      } catch {
        if (!cancelled) {
          setItems([]);
          setError("无法加载事故：projection-api 或 Runtime DB 不可用。");
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  async function refresh() {
    try {
      const next = await projection.getIncidents(50);
      setItems(next);
      setError(null);
    } catch {
      setItems([]);
      setError("无法加载事故：projection-api 或 Runtime DB 不可用。");
    }
  }

  async function emergencyStop(market: "CRYPTO" | "A_SHARE") {
    if (!canEmergencyStop) return;
    setStopping(true);
    setError(null);
    try {
      const csrf = await fetch("/api/csrf").then((r) => r.json()) as {
        csrf_token?: string;
      };
      const res = await fetch("/api/control/emergency-stop", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": csrf.csrf_token || "",
        },
        body: JSON.stringify({ market }),
      });
      if (!res.ok) {
        setError(`紧急停止失败：BFF 返回 ${res.status}。`);
      }
    } catch {
      setError("紧急停止失败：无法连接 BFF。");
    } finally {
      setStopping(false);
      await refresh();
    }
  }

  return (
    <section>
      {error && <p style={{ color: "red" }}>{error}</p>}
      {!canEmergencyStop && (
        <p data-testid="risk-operator-required" style={{ color: "#6b7280" }}>
          需要 RiskOperator 才能紧急停止。当前 Viewer 只能查看。
        </p>
      )}
      <div style={{ display: "flex", gap: 8, marginBottom: 16 }}>
        <button
          type="button"
          data-testid="stop-crypto"
          {...writeActionProps(canEmergencyStop, () => {
            void emergencyStop("CRYPTO");
          })}
          disabled={!canEmergencyStop || stopping}
          style={buttonStyle("#dc2626", !canEmergencyStop)}
        >
          Crypto 紧急停止
        </button>
        <button
          type="button"
          data-testid="stop-ashare"
          {...writeActionProps(canEmergencyStop, () => {
            void emergencyStop("A_SHARE");
          })}
          disabled={!canEmergencyStop || stopping}
          style={buttonStyle("#b45309", !canEmergencyStop)}
        >
          A 股紧急停止
        </button>
      </div>
      {items === null ? (
        <p>加载中…</p>
      ) : items.length === 0 && !error ? (
        <p>暂无事故事件。</p>
      ) : (
        <table style={{ borderCollapse: "collapse", width: "100%" }}>
          <thead>
            <tr>
              {["时间", "类型", "市场", "原因"].map((h) => (
                <th key={h} style={th}>
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {items.map((item) => (
              <tr key={item.event_id}>
                <td style={td}>{item.occurred_at}</td>
                <td style={td}>{item.event_type}</td>
                <td style={td}>{item.market}</td>
                <td style={td}>
                  {String(item.payload.reason || JSON.stringify(item.payload))}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

const th = {
  border: "1px solid #e5e7eb",
  padding: "6px 10px",
  textAlign: "left" as const,
  backgroundColor: "#f9fafb",
};
const td = { border: "1px solid #e5e7eb", padding: "6px 10px", fontSize: 14 };

function buttonStyle(color: string, disabled = false) {
  return {
    padding: "6px 16px",
    borderRadius: 6,
    border: "none",
    backgroundColor: disabled ? "#9ca3af" : color,
    color: "#ffffff",
    cursor: disabled ? "not-allowed" : "pointer",
  };
}
