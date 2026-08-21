import { NextResponse } from "next/server";
import { requireViewer } from "@/lib/identity";
import { serverServiceHeaders } from "@/lib/projection";

export const dynamic = "force-dynamic";

/** Chief 只读查询 BFF：转发投影，不接触 Gateway 写接口。 */
export async function POST(request: Request) {
  const auth = await requireViewer(request);
  if ("error" in auth) return auth.error;
  const body = await request.json();
  const projection =
    process.env.PROJECTION_API_URL || "http://127.0.0.1:8004";
  const upstream = await fetch(`${projection}/v1/chief/query`, {
    method: "POST",
    // 生产 Projection 开鉴权后必须携带服务身份,否则 401
    headers: { "Content-Type": "application/json", ...serverServiceHeaders() },
    body: JSON.stringify(body),
  });
  const text = await upstream.text();
  return new NextResponse(text, {
    status: upstream.status,
    headers: { "Content-Type": "application/json" },
  });
}
