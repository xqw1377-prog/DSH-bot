import Link from "next/link";
import { ModeBanner } from "@/components/mode-banner";
import type { GlobalMode } from "@dsh-bot/client-sdk";

const NAV_ITEMS = [
  { href: "/", label: "Bot Home" },
  { href: "/chat", label: "Chief" },
  { href: "/crypto", label: "Crypto" },
  { href: "/a-share", label: "A 股" },
  { href: "/approvals", label: "审批" },
  { href: "/tasks", label: "任务" },
  { href: "/incidents", label: "事故" },
  { href: "/portfolio", label: "Portfolio" },
  { href: "/strategy-lab", label: "Strategy Lab" },
];

/** 顶部导航。模式只展示，LIVE 不能选择。 */
export function TopNav({
  globalMode,
  liveAnomaly,
}: {
  globalMode: GlobalMode;
  liveAnomaly: boolean;
}) {
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
      {NAV_ITEMS.map((item) => (
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
