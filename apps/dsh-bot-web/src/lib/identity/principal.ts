import { NextResponse } from "next/server";
import { isDevelopmentEnv } from "./env";
import { groupsFromClaims, rolesFromGroups } from "./group-role-mapping";
import { IapJwtError, verifyIapJwt } from "./jwt";
import {
  identityNowMs,
  jwksCacheFor,
  JwksUnavailableError,
  JwksUrlError,
  UnknownKidError,
  assertConfiguredJwksUrl,
} from "./jwks";
import type { Principal, ProjectRole } from "./types";

export { isDevelopmentEnv };

export function iapConfigured(): boolean {
  return Boolean(
    process.env.DSH_IAP_ISSUER &&
      process.env.DSH_IAP_AUDIENCE &&
      process.env.DSH_IAP_JWKS_URL,
  );
}

function jsonError(detail: string, status: number): NextResponse {
  return NextResponse.json({ detail }, { status });
}

/** 外部伪造身份 Header 一律忽略；BFF 只认 IAP JWT。 */
export function extractIapToken(request: Request): string | null {
  const configured = process.env.DSH_IAP_JWT_HEADER?.trim();
  if (configured) {
    const injected = request.headers.get(configured);
    if (injected) {
      return injected.replace(/^Bearer\s+/i, "").trim() || null;
    }
  }
  const authorization = request.headers.get("authorization");
  if (authorization && /^Bearer\s+/i.test(authorization)) {
    return authorization.replace(/^Bearer\s+/i, "").trim() || null;
  }
  return null;
}

function developmentMockPrincipal(): Principal | null {
  if (!isDevelopmentEnv()) return null;
  const subject =
    process.env.DSH_DEV_USER || process.env.DSH_SESSION_USER || "dev-user";
  return {
    subject_id: subject,
    issuer: "dev://local",
    audience: "dsh-bot-console",
    roles: ["Viewer"],
    expires_at: Math.floor(Date.now() / 1000) + 3600,
    authentication_method: "development_mock",
  };
}

function principalFromClaims(
  claims: Awaited<ReturnType<typeof verifyIapJwt>>,
): Principal {
  return {
    subject_id: claims.sub,
    issuer: claims.iss,
    audience: claims.aud,
    roles: rolesFromGroups(groupsFromClaims(claims.raw)),
    session_id: claims.sid,
    auth_time: claims.auth_time,
    expires_at: claims.exp,
    authentication_method: "iap_jwt",
    assurance_level: claims.acr,
  };
}

export async function resolvePrincipal(
  request: Request,
): Promise<{ principal: Principal } | { error: NextResponse }> {
  const configured = iapConfigured();
  const token = extractIapToken(request);

  if (configured) {
    const jwksUrl = process.env.DSH_IAP_JWKS_URL as string;
    try {
      assertConfiguredJwksUrl(jwksUrl);
    } catch {
      return {
        error: jsonError(
          "identity fail-closed: production JWKS URL must be HTTPS and an allowed host",
          503,
        ),
      };
    }
    if (!token) {
      if (isDevelopmentEnv()) {
        const mock = developmentMockPrincipal();
        if (mock) return { principal: mock };
      }
      return { error: jsonError("missing iap token", 401) };
    }
    try {
      const issuer = process.env.DSH_IAP_ISSUER as string;
      const audience = process.env.DSH_IAP_AUDIENCE as string;
      const cache = jwksCacheFor(jwksUrl);
      const claims = await verifyIapJwt(token, {
        issuer,
        audience,
        resolveKey: (kid) => cache.resolve(kid),
        now: identityNowMs,
      });
      return { principal: principalFromClaims(claims) };
    } catch (error) {
      if (error instanceof JwksUrlError) {
        return {
          error: jsonError(
            "identity fail-closed: production JWKS URL must be HTTPS and an allowed host",
            503,
          ),
        };
      }
      if (error instanceof JwksUnavailableError) {
        return { error: jsonError("jwks unavailable", 503) };
      }
      if (error instanceof IapJwtError || error instanceof UnknownKidError) {
        return { error: jsonError(error.message, 401) };
      }
      return { error: jsonError("iap token rejected", 401) };
    }
  }

  if (isDevelopmentEnv()) {
    const mock = developmentMockPrincipal();
    if (mock) return { principal: mock };
  }

  return {
    error: jsonError(
      "identity fail-closed: production requires IAP issuer, audience, and JWKS",
      503,
    ),
  };
}

export function requireRole(
  principal: Principal,
  allowed: ProjectRole[],
): NextResponse | null {
  if (allowed.some((role) => principal.roles.includes(role))) {
    return null;
  }
  return jsonError(`forbidden: ${allowed.join(" or ")} role required`, 403);
}

export async function requireViewer(
  request: Request,
): Promise<{ principal: Principal } | { error: NextResponse }> {
  const resolved = await resolvePrincipal(request);
  if ("error" in resolved) return resolved;
  const denied = requireRole(resolved.principal, ["Viewer"]);
  if (denied) return { error: denied };
  return resolved;
}

