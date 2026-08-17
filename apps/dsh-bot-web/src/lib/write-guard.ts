import { NextResponse } from "next/server";

export function writeActor():
  | { actor: string }
  | { error: NextResponse } {
  const env = process.env.DSH_ENV || "production";
  if (env === "development") {
    return { actor: process.env.DSH_DEV_USER || "dev-user" };
  }
  const sessionUser = process.env.DSH_SESSION_USER || "";
  if (!sessionUser) {
    return {
      error: NextResponse.json(
        {
          detail:
            "write BFF fail-closed: production requires Session/SSO (DSH_SESSION_USER)",
        },
        { status: 503 },
      ),
    };
  }
  return { actor: sessionUser };
}

export function assertCsrf(request: Request): NextResponse | null {
  const env = process.env.DSH_ENV || "production";
  if (env === "development") {
    return null;
  }
  const origin = request.headers.get("origin");
  const allowed = process.env.DSH_WEB_ORIGIN || "";
  if (!origin || !allowed || origin !== allowed) {
    return NextResponse.json(
      { detail: "csrf origin rejected" },
      { status: 403 },
    );
  }
  return null;
}
