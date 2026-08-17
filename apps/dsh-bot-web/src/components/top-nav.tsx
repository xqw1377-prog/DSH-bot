import Link from "next/link";

const NAV_ITEMS = [
  { href: "/", label: "Bot Home" },
  { href: "/chat", label: "Chief Chat" },
  { href: "/approvals", label: "审批" },
  { href: "/tasks", label: "任务" },
  { href: "/incidents", label: "事故" },
  { href: "/portfolio", label: "Portfolio" },
  { href: "/strategy-lab", label: "Strategy Lab" },
];

/** 顶部导航栏。 */
export function TopNav() {
  return (
    <nav
      style={{
        display: "flex",
        gap: 16,
        padding: "12px 24px",
        borderBottom: "1px solid #e5e7eb",
        backgroundColor: "#fafafa",
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
    </nav>
  );
}
