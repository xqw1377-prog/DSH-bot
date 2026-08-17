import { NextResponse } from "next/server";
import { gatewayHeaders, gatewayUrl } from "@/lib/gateway-bff";
import { assertCsrf, writeActor } from "@/lib/write-guard";

export const dynamic = "force-dynamic";

/** Kill Switch BFF：actor 由服务端 principal 生成。 */
export async function POST(request: Request) {
  const csrf = assertCsrf(request);
  if (csrf) return csrf;
  const principal = writeActor();
  if ("error" in principal) return principal.error;

  const body = (await request.json()) as {
    market?: string;
    account_id?: string;
  };
  const market = body.market === "A_SHARE" ? "A_SHARE" : "CRYPTO";
  const params = new URLSearchParams();
  if (body.account_id) params.set("account_id", body.account_id);
  params.set("actor_id", principal.actor);
  const query = params.toString();
  const upstream = await fetch(
    `${gatewayUrl()}/v1/markets/${market}/emergency-stop${query ? `?${query}` : ""}`,
    { method: "POST", headers: gatewayHeaders() },
  );
  const text = await upstream.text();
  return new NextResponse(text, {
    status: upstream.status,
    headers: { "Content-Type": "application/json" },
  });
}
