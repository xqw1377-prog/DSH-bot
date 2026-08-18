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
    return claim.some((item) => item === expected);
  }
  return false;
}

function verifySignature(alg: string, data: string, signature: Buffer, jwk: Jwk): boolean {
  const key = createPublicKey({ key: jwk as JsonWebKey, format: "jwk" });
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
    resolveKey: (kid: string) => Promise<Jwk | null>;
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

  const jwk = await options.resolveKey(header.kid);
  if (!jwk) {
    throw new IapJwtError("unknown signing key");
  }

  const data = `${headerB64}.${payloadB64}`;
  const signature = decodeSegment(signatureB64 || "");
  if (!verifySignature(alg, data, signature, jwk)) {
    throw new IapJwtError("invalid signature");
  }

  const payload = parseJson(payloadB64);
  const nowSec = Math.floor((options.now ?? Date.now)() / 1000);
  const skew = options.clockSkewSeconds ?? IAP_CLOCK_SKEW_SECONDS;

  if (payload.iss !== options.issuer) {
    throw new IapJwtError("issuer mismatch");
  }
  if (!audienceMatches(payload.aud, options.audience)) {
    throw new IapJwtError("audience mismatch");
  }
  if (typeof payload.sub !== "string" || !payload.sub) {
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
