import { NextResponse } from "next/server";
import { gatewayHeaders, gatewayUrl } from "@/lib/gateway-bff";
import {
  isDevelopmentEnv,
  requireRole,
  resolvePrincipal,
} from "@/lib/identity";
import { assertCsrf, writeActor } from "@/lib/write-guard";

export const dynamic = "force-dynamic";

/** Kill Switch BFF：actor 由服务端 principal 生成。本 PR 不开放 RiskOperator。 */
export async function POST(request: Request) {
  if (isDevelopmentEnv()) {
    const csrf = assertCsrf(request);
    if (csrf) return csrf;
    const principal = writeActor();
    if ("error" in principal) return principal.error;
    return emergencyStopUpstream(request, principal.actor);
  }

  const resolved = await resolvePrincipal(request);
  if ("error" in resolved) return resolved.error;
  const denied = requireRole(resolved.principal, ["RiskOperator"]);
  if (denied) return denied;
  const csrf = assertCsrf(request);
  if (csrf) return csrf;
  return emergencyStopUpstream(
    request,
    `${resolved.principal.issuer} ${resolved.principal.subject_id}`,
  );
}

async function emergencyStopUpstream(request: Request, actor: string) {
  const body = (await request.json()) as {
    market?: string;
    account_id?: string;
  };
  const market = body.market === "A_SHARE" ? "A_SHARE" : "CRYPTO";
  const params = new URLSearchParams();
  if (body.account_id) params.set("account_id", body.account_id);
  const query = params.toString();
  const upstream = await fetch(
    `${gatewayUrl()}/v1/markets/${market}/emergency-stop${query ? `?${query}` : ""}`,
    {
      method: "POST",
      headers: gatewayHeaders({ "X-Actor-Id": actor }),
    },
  );
  const text = await upstream.text();
  return new NextResponse(text, {
    status: upstream.status,
    headers: { "Content-Type": "application/json" },
  });
}
