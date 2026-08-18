import { ReactNode } from "react";
import { TopNav } from "@/components/top-nav";
import { requirePageViewer } from "@/lib/page-auth";
import { projection } from "@/lib/projection";

export const dynamic = "force-dynamic";
export const revalidate = 0;
export const fetchCache = "force-no-store";

export default async function ConsoleLayout({ children }: { children: ReactNode }) {
  await requirePageViewer();
  const overview = await projection.getBotsOverview().catch(() => null);
  return (
    <>
      <TopNav
        globalMode={overview?.global_mode ?? "UNKNOWN"}
        liveAnomaly={overview?.live_anomaly ?? false}
      />
      {children}
    </>
  );
}
