import type { Jwk, JwksDocument } from "./types";
import { IAP_JWKS_CACHE_TTL_SECONDS } from "./types";
import { isDevelopmentEnv } from "./env";

export type JwksFetcher = (url: string) => Promise<JwksDocument>;

export class JwksUnavailableError extends Error {
  constructor() {
    super("jwks unavailable");
    this.name = "JwksUnavailableError";
  }
}

export class UnknownKidError extends Error {
  constructor() {
    super("unknown signing key");
    this.name = "UnknownKidError";
  }
}

export class JwksUrlError extends Error {
  constructor() {
    super("jwks url rejected");
    this.name = "JwksUrlError";
  }
}

function allowedJwksHosts(): string[] {
  return (process.env.DSH_IAP_JWKS_ALLOWED_HOSTS || "")
    .split(",")
    .map((host) => host.trim().toLowerCase())
    .filter(Boolean);
}

/** JWKS 地址只来自服务端配置。生产必须 HTTPS，且主机在允许列表。 */
export function assertConfiguredJwksUrl(url: string): void {
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    throw new JwksUrlError();
  }
  if (parsed.username || parsed.password) {
    throw new JwksUrlError();
  }
  if (isDevelopmentEnv()) {
    if (parsed.protocol !== "https:" && parsed.protocol !== "http:") {
      throw new JwksUrlError();
    }
    return;
  }
  if (parsed.protocol !== "https:") {
    throw new JwksUrlError();
  }
  const host = parsed.hostname.toLowerCase();
  if (!allowedJwksHosts().includes(host)) {
    throw new JwksUrlError();
  }
}

export async function defaultFetchJwks(url: string): Promise<JwksDocument> {
  assertConfiguredJwksUrl(url);
  const response = await fetch(url, { cache: "no-store", redirect: "error" });
  if (!response.ok) {
    throw new JwksUnavailableError();
  }
  const body = (await response.json()) as JwksDocument;
  if (!body || !Array.isArray(body.keys)) {
    throw new JwksUnavailableError();
  }
  return body;
}

type CachedKey = { jwk: Jwk; expiresAtMs: number };

export class JwksCache {
  constructor(
    private readonly url: string,
    private readonly fetcher: JwksFetcher,
    private readonly ttlMs: number = IAP_JWKS_CACHE_TTL_SECONDS * 1000,
    private readonly now: () => number = Date.now,
    private readonly keys = new Map<string, CachedKey>(),
  ) {}

  async resolve(kid: string): Promise<Jwk> {
    const now = this.now();
    const cached = this.keys.get(kid);
    if (cached && cached.expiresAtMs > now) {
      return cached.jwk;
    }
    if (cached && cached.expiresAtMs <= now) {
      this.keys.delete(kid);
    }

    let document: JwksDocument;
    try {
      document = await this.fetcher(this.url);
    } catch (error) {
      if (error instanceof JwksUrlError) throw error;
      throw new JwksUnavailableError();
    }

    const expiresAtMs = now + this.ttlMs;
    this.keys.clear();
    for (const jwk of document.keys) {
      if (!jwk.kid) continue;
      this.keys.set(jwk.kid, { jwk, expiresAtMs });
    }

    const fresh = this.keys.get(kid);
    if (fresh && fresh.expiresAtMs > now) {
      return fresh.jwk;
    }
    throw new UnknownKidError();
  }
}

const caches = new Map<string, JwksCache>();
let testFetcher: JwksFetcher | undefined;
let testNow: (() => number) | undefined;

export function resetJwksCacheForTests(): void {
  caches.clear();
  testFetcher = undefined;
  testNow = undefined;
}

export function setJwksTestHooks(hooks: {
  fetcher?: JwksFetcher;
  now?: () => number;
}): void {
  testFetcher = hooks.fetcher;
  testNow = hooks.now;
  caches.clear();
}

export function identityNowMs(): number {
  return (testNow ?? Date.now)();
}

export function jwksCacheFor(url: string): JwksCache {
  const existing = caches.get(url);
  if (existing) return existing;
  const created = new JwksCache(
    url,
    testFetcher ?? defaultFetchJwks,
    IAP_JWKS_CACHE_TTL_SECONDS * 1000,
    testNow ?? Date.now,
  );
  caches.set(url, created);
  return created;
}
