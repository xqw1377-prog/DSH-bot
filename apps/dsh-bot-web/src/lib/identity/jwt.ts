import { createPublicKey, createVerify, type JsonWebKey } from "node:crypto";
import { ALLOWED_IAP_ALGS, IAP_CLOCK_SKEW_SECONDS, type Jwk } from "./types";

export type VerifiedIapClaims = {
  iss: string;
  aud: string;
  sub: string;
  exp: number;
  iat?: number;
  nbf?: number;
  sid?: string;
  auth_time?: number;
  acr?: string;
  groups: unknown;
  raw: Record<string, unknown>;
};

export class IapJwtError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "IapJwtError";
  }
}

function decodeSegment(segment: string): Buffer {
  const padded = segment.replace(/-/g, "+").replace(/_/g, "/");
  const pad = padded.length % 4 === 0 ? "" : "=".repeat(4 - (padded.length % 4));
  return Buffer.from(padded + pad, "base64");
}

function parseJson(segment: string): Record<string, unknown> {
  try {
    return JSON.parse(decodeSegment(segment).toString("utf8")) as Record<string, unknown>;
  } catch {
    throw new IapJwtError("malformed jwt");
  }
}

function audienceMatches(claim: unknown, expected: string): boolean {
  if (typeof claim === "string") return claim === expected;
  if (Array.isArray(claim)) {
    return claim.length > 0 && claim.every((item) => item === expected);
  }
  return false;
}

function expectedKty(alg: string): string {
  if (alg === "RS256") return "RSA";
  if (alg === "ES256") return "EC";
  return "";
}

function assertJwkMatches(jwk: Jwk, kid: string, alg: string): void {
  if (jwk.kid !== kid) {
    throw new IapJwtError("jwk kid mismatch");
  }
  if (jwk.use !== "sig") {
    throw new IapJwtError("jwk use must be sig");
  }
  if (jwk.alg !== alg) {
    throw new IapJwtError("jwk alg mismatch");
  }
  if (jwk.kty !== expectedKty(alg)) {
    throw new IapJwtError("jwk kty mismatch");
  }
}

/** 只用公钥字段验签，忽略 Token 里的 jku/x5u/jwk/jwks_uri。 */
function publicVerifyKey(jwk: Jwk): JsonWebKey {
  return {
    kty: jwk.kty,
    kid: jwk.kid,
    alg: jwk.alg,
    use: jwk.use,
    n: jwk.n,
    e: jwk.e,
    crv: jwk.crv,
    x: jwk.x,
    y: jwk.y,
  };
}

function verifySignature(alg: string, data: string, signature: Buffer, jwk: Jwk): boolean {
  const key = createPublicKey({ key: publicVerifyKey(jwk), format: "jwk" });
  if (alg === "RS256") {
    const verifier = createVerify("RSA-SHA256");
    verifier.update(data);
    return verifier.verify(key, signature);
  }
  if (alg === "ES256") {
    const verifier = createVerify("SHA256");
    verifier.update(data);
    return verifier.verify(key, signature);
  }
  return false;
}

export async function verifyIapJwt(
  token: string,
  options: {
    issuer: string;
    audience: string;
    resolveKey: (kid: string) => Promise<Jwk>;
    now?: () => number;
    clockSkewSeconds?: number;
  },
): Promise<VerifiedIapClaims> {
  const parts = token.split(".");
  if (parts.length !== 3 || !parts[0] || !parts[1]) {
    throw new IapJwtError("malformed jwt");
  }
  const [headerB64, payloadB64, signatureB64] = parts;
  const header = parseJson(headerB64);
  const alg = header.alg;
  if (typeof alg !== "string" || !(ALLOWED_IAP_ALGS as readonly string[]).includes(alg)) {
    throw new IapJwtError("algorithm not allowed");
  }
  if (typeof header.kid !== "string" || !header.kid) {
    throw new IapJwtError("kid required");
  }

  // 不读取 header.jku / header.x5u / header.jwk / payload.jwks_uri。
  // 公钥只来自服务端配置的 JWKS。
  const jwk = await options.resolveKey(header.kid);
  assertJwkMatches(jwk, header.kid, alg);

  const data = `${headerB64}.${payloadB64}`;
  const signature = decodeSegment(signatureB64 || "");
  if (!verifySignature(alg, data, signature, jwk)) {
    throw new IapJwtError("invalid signature");
  }

  const payload = parseJson(payloadB64);
  const nowSec = Math.floor((options.now ?? Date.now)() / 1000);
  const skew = options.clockSkewSeconds ?? IAP_CLOCK_SKEW_SECONDS;

  if (typeof payload.iss !== "string" || payload.iss !== options.issuer) {
    throw new IapJwtError("issuer mismatch");
  }
  if (!audienceMatches(payload.aud, options.audience)) {
    throw new IapJwtError("audience mismatch");
  }
  if (typeof payload.sub !== "string" || !payload.sub.trim()) {
    throw new IapJwtError("subject required");
  }
  if (typeof payload.exp !== "number") {
    throw new IapJwtError("exp required");
  }
  if (payload.exp + skew < nowSec) {
    throw new IapJwtError("token expired");
  }
  if (typeof payload.nbf === "number" && payload.nbf - skew > nowSec) {
    throw new IapJwtError("token not yet valid");
  }
  if (typeof payload.iat === "number" && payload.iat - skew > nowSec) {
    throw new IapJwtError("iat in the future");
  }

  return {
    iss: options.issuer,
    aud: options.audience,
    sub: payload.sub,
    exp: payload.exp,
    iat: typeof payload.iat === "number" ? payload.iat : undefined,
    nbf: typeof payload.nbf === "number" ? payload.nbf : undefined,
    sid: typeof payload.sid === "string" ? payload.sid : undefined,
    auth_time: typeof payload.auth_time === "number" ? payload.auth_time : undefined,
    acr: typeof payload.acr === "string" ? payload.acr : undefined,
    groups: payload.groups,
    raw: payload,
  };
}
