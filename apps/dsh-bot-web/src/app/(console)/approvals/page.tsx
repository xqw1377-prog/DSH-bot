import { ApprovalsPanel } from "@/components/approvals-panel";
import { capabilitiesFrom, requirePageViewer } from "@/lib/page-auth";

export const dynamic = "force-dynamic";

export default async function ApprovalsPage() {
  const principal = await requirePageViewer();
  const caps = capabilitiesFrom(principal);
  return (
    <main style={{ padding: 24 }}>
      <h1>审批中心</h1>
      <p>只读查看待审批项。资金决定仍由 BFF/Gateway 授权，不由浏览器判断。</p>
      <ApprovalsPanel canDecide={caps.canDecide} />
    </main>
  );
}
