import { NextResponse } from "next/server";
import { gatewayHeaders, gatewayUrl } from "@/lib/gateway-bff";
import { assertCsrf, writeActor } from "@/lib/write-guard";

export const dynamic = "force-dynamic";

/** 审批 BFF：浏览器不持 Gateway Key，也不得提交 decided_by。 */
export async function POST(
  request: Request,
  context: { params: Promise<{ id: string }> },
) {
  const csrf = assertCsrf(request);
  if (csrf) return csrf;
  const principal = writeActor();
  if ("error" in principal) return principal.error;

  const { id } = await context.params;
  const body = (await request.json()) as { decision?: string };
  const upstream = await fetch(`${gatewayUrl()}/v1/approvals/${id}/decide`, {
    method: "POST",
    headers: gatewayHeaders(),
    body: JSON.stringify({
      decision: body.decision,
      decided_by: principal.actor,
    }),
  });
  const text = await upstream.text();
  return new NextResponse(text, {
    status: upstream.status,
    headers: { "Content-Type": "application/json" },
  });
}
