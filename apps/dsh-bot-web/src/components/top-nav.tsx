import Link from "next/link";
import { ModeBanner } from "@/components/mode-banner";
import type { GlobalMode } from "@dsh-bot/client-sdk";

const NAV_ITEMS = [
  { href: "/", label: "今日" },
  { href: "/shadow", label: "决策" },
  { href: "/audit", label: "审计" },
  { href: "/intel", label: "情报" },
  { href: "/crypto", label: "Crypto" },
  { href: "/a-share", label: "A 股" },
  { href: "/chat", label: "Chief" },
  { href: "/tasks", label: "任务" },
  { href: "/incidents", label: "事故" },
];

const PAPER_ONLY_NAV = [
  { href: "/approvals", label: "审批" },
  { href: "/portfolio", label: "组合" },
  { href: "/strategy-lab", label: "实验室" },
];

/** 顶部导航。模式只展示，LIVE 不能选择。 */
export function TopNav({
  globalMode,
  liveAnomaly,
}: {
  globalMode: GlobalMode;
  liveAnomaly: boolean;
}) {
  const items =
    globalMode === "SHADOW" ? NAV_ITEMS : [...NAV_ITEMS, ...PAPER_ONLY_NAV];
  return (
    <nav
      style={{
        display: "flex",
        gap: 16,
        padding: "12px 24px",
        borderBottom: "1px solid #e5e7eb",
        backgroundColor: "#fafafa",
        alignItems: "center",
      }}
    >
      {items.map((item) => (
        <Link
          key={item.href}
          href={item.href}
          style={{ color: "#111827", textDecoration: "none", fontWeight: 500 }}
        >
          {item.label}
        </Link>
      ))}
      <ModeBanner globalMode={globalMode} liveAnomaly={liveAnomaly} />
    </nav>
  );
}
