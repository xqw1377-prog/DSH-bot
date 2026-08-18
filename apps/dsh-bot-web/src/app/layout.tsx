import { ReactNode } from "react";

export const metadata = {
  title: "DSH Bot",
  description: "持续进化量化 Agent 平台",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="zh-CN">
      <body style={{ margin: 0, fontFamily: "system-ui, sans-serif" }}>
        {children}
      </body>
    </html>
  );
}
