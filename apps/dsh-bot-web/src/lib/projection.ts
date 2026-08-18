import { ProjectionClient } from "@dsh-bot/client-sdk";

function projectionBase(): string {
  if (typeof window === "undefined") {
    return process.env.PROJECTION_API_URL || "http://127.0.0.1:8004";
  }
  return "/api/projection";
}

/** 浏览器只走 Next BFF；RSC 先验 Viewer，再用服务身份直连 Projection。 */
export const projection = new ProjectionClient(projectionBase());
