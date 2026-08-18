import { createHmac, randomBytes } from "node:crypto";
import { NextResponse } from "next/server";

const CSRF_COOKIE = "dsh_csrf";

function csrfSecret(): string {
  return process.env.DSH_CSRF_SECRET || process.env.DSH_SESSION_USER || "dev-csrf";
}

export function issueCsrf(): { token: string; cookie: string } {
  const nonce = randomBytes(16).toString("hex");
  const mac = createHmac("sha256", csrfSecret()).update(nonce).digest("hex");
  const token = `${nonce}.${mac}`;
  const secure = process.env.DSH_ENV === "production" ? "; Secure" : "";
  const cookie = `${CSRF_COOKIE}=${token}; Path=/; HttpOnly; SameSite=Strict${secure}`;
  return { token, cookie };
}

function parseCookie(header: string | null, name: string): string {
  if (!header) return "";
  for (const part of header.split(";")) {
    const [k, ...rest] = part.trim().split("=");
    if (k === name) return rest.join("=");
  }
  return "";
}

export function writeActor():
  | { actor: string }
  | { error: NextResponse } {
  const env = process.env.DSH_ENV || "production";
  if (env === "development") {
    return {
      actor: process.env.DSH_DEV_USER || process.env.DSH_SESSION_USER || "dev-user",
    };
  }
  return {
    error: NextResponse.json(
      {
        detail:
          "write BFF fail-closed: production requires IAP principal; DSH_SESSION_USER is development-only",
      },
      { status: 503 },
    ),
  };
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
  const headerToken = request.headers.get("x-csrf-token") || "";
  const cookieToken = parseCookie(request.headers.get("cookie"), CSRF_COOKIE);
  if (!headerToken || !cookieToken || headerToken !== cookieToken) {
    return NextResponse.json(
      { detail: "csrf token rejected" },
      { status: 403 },
    );
  }
  const [nonce, mac] = headerToken.split(".");
  if (!nonce || !mac) {
    return NextResponse.json({ detail: "csrf token rejected" }, { status: 403 });
  }
  const expected = createHmac("sha256", csrfSecret()).update(nonce).digest("hex");
  if (expected !== mac) {
    return NextResponse.json({ detail: "csrf token rejected" }, { status: 403 });
  }
  return null;
}
