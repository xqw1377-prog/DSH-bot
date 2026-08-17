import { NextResponse } from "next/server";

export const dynamic = "force-dynamic";

/** 审批 BFF：浏览器不持有 Gateway API Key。 */
export async function POST(
  request: Request,
  context: { params: Promise<{ id: string }> },
) {
  const { id } = await context.params;
  const body = await request.json();
  const gateway =
    process.env.QUANT_GATEWAY_URL || "http://127.0.0.1:8001";
  const apiKey = process.env.QUANT_GATEWAY_API_KEY || "";
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
  };
  if (apiKey) {
    headers["X-API-Key"] = apiKey;
  }
  const upstream = await fetch(`${gateway}/v1/approvals/${id}/decide`, {
    method: "POST",
    headers,
    body: JSON.stringify(body),
  });
  const text = await upstream.text();
  return new NextResponse(text, {
    status: upstream.status,
    headers: { "Content-Type": "application/json" },
  });
}
