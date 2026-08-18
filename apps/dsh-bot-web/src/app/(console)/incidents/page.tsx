import { IncidentsPanel } from "@/components/incidents-panel";
import { capabilitiesFrom, requirePageViewer } from "@/lib/page-auth";

export const dynamic = "force-dynamic";

export default async function IncidentsPage() {
  const principal = await requirePageViewer();
  const caps = capabilitiesFrom(principal);
  return (
    <main style={{ padding: 24 }}>
      <h1>事故与 Kill Switch</h1>
      <p>
        对账 MISMATCH、UNKNOWN 超时、审批账本异常。自动 Kill Switch 仅来自
        risk-policy CRITICAL。
      </p>
      <IncidentsPanel canEmergencyStop={caps.canEmergencyStop} />
    </main>
  );
}
