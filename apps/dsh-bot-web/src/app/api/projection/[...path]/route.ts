import { NextResponse } from "next/server";
import { requireViewer } from "@/lib/identity";

export const dynamic = "force-dynamic";

function upstream(path: string[], search: string): string {
  const base = process.env.PROJECTION_API_URL || "http://127.0.0.1:8004";
  const suffix = path.join("/");
  return `${base}/${suffix}${search}`;
}

function serviceIdentityHeaders(): HeadersInit {
  const key = process.env.PROJECTION_API_KEY || "";
  return key ? { "X-API-Key": key } : {};
}

async function proxy(request: Request, path: string[]): Promise<NextResponse> {
  const url = new URL(request.url);
  const target = upstream(path, url.search);
  const init: RequestInit = {
    method: request.method,
    cache: "no-store",
    headers: serviceIdentityHeaders(),
  };
  if (request.method !== "GET" && request.method !== "HEAD") {
    init.headers = {
      ...serviceIdentityHeaders(),
      "Content-Type": "application/json",
    };
    init.body = await request.text();
  }
  const upstreamResp = await fetch(target, init);
  const text = await upstreamResp.text();
  return new NextResponse(text, {
    status: upstreamResp.status,
    headers: { "Content-Type": "application/json" },
  });
}

export async function GET(
  request: Request,
  context: { params: Promise<{ path: string[] }> },
) {
  const auth = await requireViewer(request);
  if ("error" in auth) return auth.error;
  const { path } = await context.params;
  return proxy(request, path);
}

export async function POST() {
  return NextResponse.json(
    { detail: "projection BFF is read-only" },
    { status: 405 },
  );
}
