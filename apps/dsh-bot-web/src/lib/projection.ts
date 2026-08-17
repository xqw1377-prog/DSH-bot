import { ProjectionClient } from "@dsh-bot/client-sdk";

/** 全局唯一的只读投影客户端。资金动作不经过此客户端，必须走审批流程。 */
export const projection = new ProjectionClient(
  process.env.PROJECTION_API_URL || "http://127.0.0.1:8004"
);

