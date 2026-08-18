import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { BotConsole } from "@/components/bot-console";
import { IncidentsPanel } from "@/components/incidents-panel";
import { ModeBanner } from "@/components/mode-banner";
import type { BotsOverview } from "@dsh-bot/client-sdk";
import {
  capabilitiesFrom,
  dataLooksHealthy,
  hasLiveSelector,
} from "@/lib/console-view";
import type { Principal } from "@/lib/identity";

vi.mock("next/link", () => ({
  default: ({
    href,
    children,
    ...props
  }: {
    href: string;
    children: React.ReactNode;
  }) => (
    <a href={href} {...props}>
      {children}
    </a>
  ),
}));

const overview = (over: Partial<BotsOverview> = {}): BotsOverview => ({
  as_of: "2026-08-18T00:00:00Z",
  global_mode: "MIXED",
  live_anomaly: false,
  alerts: [],
  bots: [
    {
      bot_id: "market-chief",
      label: "Market Chief",
      market: null,
      read_only: true,
      as_of: "t0",
      runtime: "ONLINE",
      mode: "MIXED",
      data: "FRESH",
      task: "IDLE",
      order: "NONE",
      risk: "NORMAL",
      clock_skew_ms: 3,
      degraded: false,
      connection: "CONNECTED",
      counts: {
        pending_approvals: 0,
        open_orders: 0,
        unknown_orders: 0,
        incidents: 0,
      },
    },
    {
      bot_id: "crypto",
      label: "Crypto Bot",
      market: "CRYPTO",
      read_only: false,
      as_of: "t1",
      runtime: "ONLINE",
      mode: "PAPER",
      data: "STALE",
      task: "IDLE",
      order: "UNKNOWN",
      risk: "HALTED",
      clock_skew_ms: 12,
      degraded: true,
      detail: "feed lag",
      connection: "CONNECTED",
      counts: {
        pending_approvals: 2,
        open_orders: 1,
        unknown_orders: 1,
        incidents: 1,
      },
    },
    {
      bot_id: "a-share",
      label: "A 股 Bot",
      market: "A_SHARE",
      read_only: false,
      as_of: "t2",
      runtime: "DEGRADED",
      mode: "SHADOW",
      data: "FRESH",
      task: "AWAITING_APPROVAL",
      order: "NONE",
      risk: "WARNING",
      clock_skew_ms: 4,
      degraded: true,
      connection: "CONNECTED",
      counts: {
        pending_approvals: 1,
        open_orders: 0,
        unknown_orders: 0,
        incidents: 0,
      },
    },
  ],
  ...over,
});

const viewer: Principal = {
  subject_id: "u1",
  issuer: "https://iap.test",
  audience: "dsh-bot-console",
  roles: ["Viewer"],
  expires_at: 1,
  authentication_method: "iap_jwt",
};

describe("只读三 Bot 控制台", () => {
  it("首页固定三张 Bot 卡并展示六维与 as_of", () => {
    const html = renderToStaticMarkup(
      <BotConsole
        overview={overview({
          alerts: ["Crypto Bot HALTED", "Crypto Bot UNKNOWN"],
        })}
      />,
    );
    expect(html).toContain("bot-card-market-chief");
    expect(html).toContain("bot-card-crypto");
    expect(html).toContain("bot-card-a-share");
    expect(html).toContain("READ ONLY");
    expect(html).toContain("as_of t1");
    expect(html).toContain("Runtime");
    expect(html).toContain("STALE");
    expect(html).toContain("HALTED");
    expect(html).toContain("console-alerts");
  });

  it("数据过期显示 STALE，不能当绿色 FRESH", () => {
    expect(dataLooksHealthy("STALE")).toBe(false);
    expect(dataLooksHealthy("DISCONNECTED")).toBe(false);
    expect(dataLooksHealthy("FRESH")).toBe(true);
    const html = renderToStaticMarkup(<BotConsole overview={overview()} />);
    expect(html).toContain('data-testid="dim-crypto-data"');
    expect(html).toContain(">STALE<");
    expect(html).toMatch(
      /data-testid="dim-crypto-data"[^>]*color:#b91c1c[^>]*>STALE</,
    );
    expect(html).not.toMatch(
      /data-testid="dim-crypto-data"[^>]*color:#15803d[^>]*>FRESH</,
    );
  });

  it("Viewer 写控件禁用，能力来自服务端 Principal", () => {
    const caps = capabilitiesFrom(viewer);
    expect(caps.canDecide).toBe(false);
    expect(caps.canEmergencyStop).toBe(false);
    const html = renderToStaticMarkup(
      <IncidentsPanel canEmergencyStop={caps.canEmergencyStop} />,
    );
    expect(html).toContain("risk-operator-required");
    expect(html).toMatch(/disabled="" data-testid="stop-crypto"/);
    expect(html).toMatch(/disabled="" data-testid="stop-ashare"/);
  });

  it("展示 GLOBAL MODE，LIVE 只作为异常且不是选择器", () => {
    const mixed = renderToStaticMarkup(
      <ModeBanner globalMode="MIXED" liveAnomaly={false} />,
    );
    expect(mixed).toContain("GLOBAL MODE: MIXED");
    expect(hasLiveSelector(mixed)).toBe(false);

    const live = renderToStaticMarkup(
      <ModeBanner globalMode="PAPER" liveAnomaly />,
    );
    expect(live).toContain("LIVE 异常");
    expect(hasLiveSelector(live)).toBe(false);
    expect(live).not.toContain("<select");
    expect(live).not.toContain("<option");
  });
});
