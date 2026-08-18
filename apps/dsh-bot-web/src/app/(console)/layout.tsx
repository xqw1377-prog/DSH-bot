import { ReactNode } from "react";
import { TopNav } from "@/components/top-nav";
import { requirePageViewer } from "@/lib/page-auth";
import { projection } from "@/lib/projection";

export const dynamic = "force-dynamic";

export default async function ConsoleLayout({ children }: { children: ReactNode }) {
  await requirePageViewer();
  const overview = await projection.getBotsOverview().catch(() => null);
  return (
    <>
      <TopNav
        globalMode={overview?.global_mode ?? "PAPER"}
        liveAnomaly={overview?.live_anomaly ?? false}
      />
      {children}
    </>
  );
}
