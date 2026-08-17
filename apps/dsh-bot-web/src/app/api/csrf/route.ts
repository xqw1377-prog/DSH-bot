import { NextResponse } from "next/server";
import { issueCsrf } from "@/lib/write-guard";

export const dynamic = "force-dynamic";

export async function GET() {
  const { token, cookie } = issueCsrf();
  const res = NextResponse.json({ csrf_token: token });
  res.headers.set("Set-Cookie", cookie);
  return res;
}
