import { ApprovalActionsClient, ProjectionClient } from "@dsh-bot/client-sdk";

/** 全局唯一的只读投影客户端。资金动作不经过此客户端，必须走审批流程。 */
export const projection = new ProjectionClient(
  process.env.PROJECTION_API_URL || "http://127.0.0.1:8004"
);

/**
 * 审批动作客户端：提交审批决定到 Quant Gateway。
 * 通过 SDK 封装，前端组件不直接裸 fetch 网关地址，保证分层与可测试性。
 */
export const approvalActions = new ApprovalActionsClient(
  process.env.NEXT_PUBLIC_QUANT_GATEWAY_URL || "http://127.0.0.1:8001"
);
