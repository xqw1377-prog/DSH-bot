import type { Jwk, JwksDocument } from "./types";
import { IAP_JWKS_CACHE_TTL_SECONDS } from "./types";

export type JwksFetcher = (url: string) => Promise<JwksDocument>;

export async function defaultFetchJwks(url: string): Promise<JwksDocument> {
  const response = await fetch(url, { cache: "no-store" });
  if (!response.ok) {
    throw new Error(`jwks fetch failed: ${response.status}`);
  }
  const body = (await response.json()) as JwksDocument;
  if (!body || !Array.isArray(body.keys)) {
    throw new Error("jwks document missing keys");
  }
  return body;
}

type CachedKey = { jwk: Jwk; expiresAtMs: number };

export class JwksCache {
  private readonly keys = new Map<string, CachedKey>();

  constructor(
    private readonly url: string,
    private readonly fetcher: JwksFetcher,
    private readonly ttlMs: number = IAP_JWKS_CACHE_TTL_SECONDS * 1000,
    private readonly now: () => number = Date.now,
  ) {}

  async resolve(kid: string): Promise<Jwk | null> {
    const now = this.now();
    const cached = this.keys.get(kid);
    try {
      const document = await this.fetcher(this.url);
      const expiresAtMs = now + this.ttlMs;
      for (const jwk of document.keys) {
        if (!jwk.kid) continue;
        this.keys.set(jwk.kid, { jwk, expiresAtMs });
      }
    } catch {
      if (cached && cached.expiresAtMs > now) {
        return cached.jwk;
      }
      return null;
    }

    const fresh = this.keys.get(kid);
    if (fresh && fresh.expiresAtMs > now) {
      return fresh.jwk;
    }
    return null;
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
