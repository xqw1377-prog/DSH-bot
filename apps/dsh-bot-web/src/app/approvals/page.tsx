import { ApprovalsPanel } from "@/components/approvals-panel";

// 审批列表必须实时获取。
export const dynamic = "force-dynamic";

export default function ApprovalsPage() {
  return (
    <main style={{ padding: 24 }}>
      <h1>审批中心</h1>
      <p>批准或拒绝 Bot 发起的资金与晋级动作。决定直接提交 Quant Gateway。</p>
      <ApprovalsPanel />
    </main>
  );
}
