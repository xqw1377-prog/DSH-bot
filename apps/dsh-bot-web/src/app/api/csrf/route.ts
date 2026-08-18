import { NextResponse } from "next/server";
import { requireViewer } from "@/lib/identity";
import { issueCsrf } from "@/lib/write-guard";

export const dynamic = "force-dynamic";

export async function GET(request: Request) {
  const auth = await requireViewer(request);
  if ("error" in auth) return auth.error;
  const { token, cookie } = issueCsrf();
  const res = NextResponse.json({ csrf_token: token });
  res.headers.set("Set-Cookie", cookie);
  return res;
}
