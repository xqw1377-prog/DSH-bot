import { ProjectionClient } from "@dsh-bot/client-sdk";

const client = new ProjectionClient(
  process.env.PROJECTION_API_URL || "http://127.0.0.1:8004"
);

export async function MarketOverview() {
  const [aShareHealth, cryptoHealth] = await Promise.all([
    client.getHealth("A_SHARE").catch(() => null),
    client.getHealth("CRYPTO").catch(() => null),
  ]);

  return (
    <section style={{ marginTop: 24 }}>
      <h2>市场状态</h2>
      <div style={{ display: "grid", gap: 16, gridTemplateColumns: "1fr 1fr" }}>
        <HealthCard market="A 股" health={aShareHealth} />
        <HealthCard market="数字资产" health={cryptoHealth} />
      </div>
    </section>
  );
}

function HealthCard({
  market,
  health,
}: {
  market: string;
  health: { system_ok: boolean } | null;
}) {
  return (
    <div
      style={{
        padding: 16,
        border: "1px solid #e5e7eb",
        borderRadius: 8,
      }}
    >
      <h3>{market}</h3>
      {health ? (
        <p>
          系统状态：
          <span style={{ color: health.system_ok ? "green" : "red" }}>
            {health.system_ok ? "正常" : "异常"}
          </span>
        </p>
      ) : (
        <p style={{ color: "red" }}>无法获取状态</p>
      )}
    </div>
  );
}
