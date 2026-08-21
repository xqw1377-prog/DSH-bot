import { NextResponse } from "next/server";
import { gatewayHeaders, gatewayUrl } from "@/lib/gateway-bff";
import {
  isDevelopmentEnv,
  requireRole,
  resolvePrincipal,
} from "@/lib/identity";
import { assertCsrf, writeActor } from "@/lib/write-guard";

export const dynamic = "force-dynamic";

/** 审批 BFF：浏览器不持 Gateway Key，也不得提交 decided_by。 */
export async function POST(
  request: Request,
  context: { params: Promise<{ id: string }> },
) {
  if (isDevelopmentEnv()) {
    const csrf = assertCsrf(request);
    if (csrf) return csrf;
    const principal = writeActor();
    if ("error" in principal) return principal.error;
    return decideUpstream(request, context, principal.actor);
  }

  const resolved = await resolvePrincipal(request);
  if ("error" in resolved) return resolved.error;
  const denied = requireRole(resolved.principal, ["Approver"]);
  if (denied) return denied;
  const csrf = assertCsrf(request);
  if (csrf) return csrf;
  return decideUpstream(
    request,
    context,
    `${resolved.principal.issuer} ${resolved.principal.subject_id}`,
  );
}

async function decideUpstream(
  request: Request,
  context: { params: Promise<{ id: string }> },
  actor: string,
) {
  const { id } = await context.params;
  const body = (await request.json()) as { decision?: string };
  const upstream = await fetch(`${gatewayUrl()}/v1/approvals/${id}/decide`, {
    method: "POST",
    headers: gatewayHeaders({ "X-Actor-Id": actor }),
    body: JSON.stringify({
      decision: body.decision,
    }),
  });
  const text = await upstream.text();
  return new NextResponse(text, {
    status: upstream.status,
    headers: { "Content-Type": "application/json" },
  });
}
