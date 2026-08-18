import { NextResponse } from "next/server";
import { requireViewer } from "@/lib/identity";

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
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const text = await upstream.text();
  return new NextResponse(text, {
    status: upstream.status,
    headers: { "Content-Type": "application/json" },
  });
}
