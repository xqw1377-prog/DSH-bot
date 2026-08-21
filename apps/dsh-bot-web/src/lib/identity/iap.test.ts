import { generateKeyPairSync, createSign, type KeyObject } from "node:crypto";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { POST as decideApproval } from "@/app/api/approvals/[id]/decide/route";
import { POST as emergencyStop } from "@/app/api/control/emergency-stop/route";
import { GET as getProjection } from "@/app/api/projection/[...path]/route";
import { resetJwksCacheForTests, setJwksTestHooks } from "@/lib/identity";
import { rolesFromGroups } from "@/lib/identity/group-role-mapping";
import { IAP_JWKS_CACHE_TTL_SECONDS, type Jwk } from "./types";

const ISSUER = "https://iap.test";
const AUDIENCE = "dsh-bot-console";
const NOW_MS = 1_700_000_000_000;
const NOW_SEC = Math.floor(NOW_MS / 1000);

type TestKey = {
  kid: string;
  privateKey: KeyObject;
  publicJwk: Jwk;
};

function b64urlJson(value: unknown): string {
  return Buffer.from(JSON.stringify(value))
    .toString("base64")
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/g, "");
}

function generateKey(kid: string): TestKey {
  const pair = generateKeyPairSync("rsa", { modulusLength: 2048 });
  const publicJwk = pair.publicKey.export({ format: "jwk" }) as Jwk;
  publicJwk.kid = kid;
  publicJwk.alg = "RS256";
  publicJwk.use = "sig";
  return { kid, privateKey: pair.privateKey, publicJwk };
}

function signJwt(
  key: TestKey,
  claims: Record<string, unknown>,
  header: Record<string, unknown> = {},
): string {
  const encodedHeader = b64urlJson({
    alg: "RS256",
    kid: key.kid,
    typ: "JWT",
    ...header,
  });
  const encodedPayload = b64urlJson({
    iss: ISSUER,
    aud: AUDIENCE,
    sub: "user-1",
    iat: NOW_SEC,
    exp: NOW_SEC + 3600,
    groups: ["dsh-viewers"],
    ...claims,
  });
  const data = `${encodedHeader}.${encodedPayload}`;
  const signer = createSign("RSA-SHA256");
  signer.update(data);
  const signature = signer
    .sign(key.privateKey)
    .toString("base64")
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/g, "");
  return `${data}.${signature}`;
}

function unsignedJwt(header: Record<string, unknown>, claims: Record<string, unknown>): string {
  return `${b64urlJson(header)}.${b64urlJson({
    iss: ISSUER,
    aud: AUDIENCE,
    sub: "user-1",
    exp: NOW_SEC + 300,
    groups: ["dsh-viewers"],
    ...claims,
  })}.`;
}

const KEY_A = generateKey("kid-a");
const KEY_B = generateKey("kid-b");

const IAP_ENV = {
  DSH_ENV: "production",
  DSH_IAP_ISSUER: ISSUER,
  DSH_IAP_AUDIENCE: AUDIENCE,
  DSH_IAP_JWKS_URL: "https://jwks.test/jwks.json",
  DSH_IAP_JWKS_ALLOWED_HOSTS: "jwks.test",
};

const savedEnv = { ...process.env };
const realFetch = globalThis.fetch;
let gatewayCalls = 0;
let jwksCalls = 0;
let currentNow = NOW_MS;
let fetchedUrls: string[] = [];
let jwksImpl: () => Promise<{ keys: Jwk[] }> = async () => ({
  keys: [KEY_A.publicJwk],
});

function applyEnv(extra: Record<string, string | undefined> = {}): void {
  for (const key of Object.keys(process.env)) {
    if (
      key.startsWith("DSH_") ||
      key === "PROJECTION_API_URL" ||
      key === "PROJECTION_API_KEY" ||
      key === "QUANT_GATEWAY_URL"
    ) {
      delete process.env[key];
    }
  }
  Object.assign(process.env, IAP_ENV, extra);
}

function bearer(token: string, extra: Record<string, string> = {}): HeadersInit {
  return { authorization: `Bearer ${token}`, ...extra };
}

function projectionRequest(headers?: HeadersInit): Request {
  return new Request("http://bff.test/api/projection/v1/health", { headers });
}

beforeEach(() => {
  applyEnv();
  gatewayCalls = 0;
  jwksCalls = 0;
  currentNow = NOW_MS;
  fetchedUrls = [];
  jwksImpl = async () => ({ keys: [KEY_A.publicJwk] });
  resetJwksCacheForTests();
  setJwksTestHooks({
    now: () => currentNow,
    fetcher: async (url) => {
      jwksCalls += 1;
      fetchedUrls.push(url);
      return jwksImpl();
    },
  });
  globalThis.fetch = (async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes("/v1/approvals/") || url.includes("emergency-stop")) {
      gatewayCalls += 1;
      return new Response(JSON.stringify({ ok: true }), { status: 200 });
    }
    return new Response(JSON.stringify({ system_ok: true }), { status: 200 });
  }) as typeof fetch;
});

afterEach(() => {
  process.env = { ...savedEnv };
  resetJwksCacheForTests();
  globalThis.fetch = realFetch;
});

describe("IAP Viewer BFF", () => {
  it("合法 Viewer Token 可以读取投影", async () => {
    const token = signJwt(KEY_A, {});
    const response = await getProjection(projectionRequest(bearer(token)), {
      params: Promise.resolve({ path: ["v1", "health"] }),
    });
    expect(response.status).toBe(200);
    await expect(response.json()).resolves.toEqual({ system_ok: true });
  });

  it("缺 Token 返回 401", async () => {
    const response = await getProjection(projectionRequest(), {
      params: Promise.resolve({ path: ["v1", "health"] }),
    });
    expect(response.status).toBe(401);
  });

  it("错误签名 / issuer / audience 返回 401", async () => {
    const wrongSig = signJwt(KEY_B, {});
    const wrongIss = signJwt(KEY_A, { iss: "https://evil.test" });
    const wrongAud = signJwt(KEY_A, { aud: "other-app" });
    const expired = signJwt(KEY_A, { exp: NOW_SEC - 120 });
    const notYet = signJwt(KEY_A, { nbf: NOW_SEC + 120 });
    const emptySub = signJwt(KEY_A, { sub: "   " });
    const noneAlg = unsignedJwt({ alg: "none", kid: "kid-a" }, {});

    for (const token of [wrongSig, wrongIss, wrongAud, expired, notYet, emptySub, noneAlg]) {
      const response = await getProjection(projectionRequest(bearer(token)), {
        params: Promise.resolve({ path: ["v1", "health"] }),
      });
      expect(response.status).toBe(401);
    }
  });

  it("Viewer 调用写接口返回 403，且不打 Gateway", async () => {
    const token = signJwt(KEY_A, {});
    const decide = await decideApproval(
      new Request("http://bff.test/api/approvals/appr-1/decide", {
        method: "POST",
        headers: bearer(token, { "content-type": "application/json" }),
        body: JSON.stringify({ decision: "APPROVE", decided_by: "forged" }),
      }),
      { params: Promise.resolve({ id: "appr-1" }) },
    );
    const stop = await emergencyStop(
      new Request("http://bff.test/api/control/emergency-stop", {
        method: "POST",
        headers: bearer(token, { "content-type": "application/json" }),
        body: JSON.stringify({ market: "CRYPTO" }),
      }),
    );
    expect(decide.status).toBe(403);
    expect(stop.status).toBe(403);
    expect(gatewayCalls).toBe(0);
  });

  it("外部伪造身份 Header 不影响 principal", async () => {
    const token = signJwt(KEY_A, { sub: "real-sub" });
    const response = await getProjection(
      projectionRequest(
        bearer(token, {
          "x-user": "admin",
          "x-forwarded-user": "admin@example.com",
          decided_by: "forged-approver",
        }),
      ),
      { params: Promise.resolve({ path: ["v1", "health"] }) },
    );
    expect(response.status).toBe(200);

    const forgedOnly = await getProjection(
      projectionRequest({
        "x-user": "admin",
        "x-forwarded-email": "admin@example.com",
        decided_by: "forged-approver",
      }),
      { params: Promise.resolve({ path: ["v1", "health"] }) },
    );
    expect(forgedOnly.status).toBe(401);
  });

  it("JWKS 暂不可用时未过期缓存 Key 可继续验证；未知 Key 失败关闭", async () => {
    const token = signJwt(KEY_A, {});
    const first = await getProjection(projectionRequest(bearer(token)), {
      params: Promise.resolve({ path: ["v1", "health"] }),
    });
    expect(first.status).toBe(200);
    expect(jwksCalls).toBe(1);

    jwksImpl = async () => {
      throw new Error("jwks unavailable");
    };

    const cached = await getProjection(projectionRequest(bearer(token)), {
      params: Promise.resolve({ path: ["v1", "health"] }),
    });
    expect(cached.status).toBe(200);
    expect(jwksCalls).toBe(1);

    const unknown = await getProjection(
      projectionRequest(bearer(signJwt(KEY_B, {}))),
      { params: Promise.resolve({ path: ["v1", "health"] }) },
    );
    expect(unknown.status).toBe(503);
    expect(jwksCalls).toBe(2);
  });

  it("development 模拟身份不能在 production 启用", async () => {
    applyEnv({
      DSH_ENV: "production",
      DSH_DEV_USER: "dev-admin",
      DSH_SESSION_USER: "session-admin",
    });
    delete process.env.DSH_IAP_ISSUER;
    delete process.env.DSH_IAP_AUDIENCE;
    delete process.env.DSH_IAP_JWKS_URL;

    const noIap = await getProjection(projectionRequest(), {
      params: Promise.resolve({ path: ["v1", "health"] }),
    });
    expect(noIap.status).toBe(503);

    applyEnv({
      DSH_ENV: "production",
      DSH_DEV_USER: "dev-admin",
      DSH_SESSION_USER: "session-admin",
    });
    const ignoredMock = await getProjection(projectionRequest(), {
      params: Promise.resolve({ path: ["v1", "health"] }),
    });
    expect(ignoredMock.status).toBe(401);

    const decide = await decideApproval(
      new Request("http://bff.test/api/approvals/appr-1/decide", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ decision: "APPROVE" }),
      }),
      { params: Promise.resolve({ id: "appr-1" }) },
    );
    expect(decide.status).toBe(401);
    expect(gatewayCalls).toBe(0);
  });

  it("未映射 Group 的合法 JWT 不能当 Viewer 读", async () => {
    // dsh-approvers 在 ADR-003 v2 已映射;真正未知的组才应拒绝
    const token = signJwt(KEY_A, { groups: ["random-sso-group"] });
    const response = await getProjection(projectionRequest(bearer(token)), {
      params: Promise.resolve({ path: ["v1", "health"] }),
    });
    expect(response.status).toBe(403);
  });

  it("未知 kid 最多强制刷新一次，JWKS 有文档仍未知则 401", async () => {
    const response = await getProjection(
      projectionRequest(bearer(signJwt(KEY_B, {}))),
      { params: Promise.resolve({ path: ["v1", "health"] }) },
    );
    expect(response.status).toBe(401);
    expect(jwksCalls).toBe(1);
    expect(fetchedUrls).toEqual(["https://jwks.test/jwks.json"]);
  });

  it("缓存 Key 硬过期后即使 JWKS 不可达也不能继续使用", async () => {
    const token = signJwt(KEY_A, {});
    const first = await getProjection(projectionRequest(bearer(token)), {
      params: Promise.resolve({ path: ["v1", "health"] }),
    });
    expect(first.status).toBe(200);

    currentNow = NOW_MS + (IAP_JWKS_CACHE_TTL_SECONDS + 1) * 1000;
    jwksImpl = async () => {
      throw new Error("jwks unavailable");
    };
    const expiredCache = await getProjection(projectionRequest(bearer(token)), {
      params: Promise.resolve({ path: ["v1", "health"] }),
    });
    expect(expiredCache.status).toBe(503);
  });

  it("Token 中的 jku/x5u 不能改写 JWKS 地址", async () => {
    const token = signJwt(KEY_A, {}, {
      jku: "https://evil.test/jwks.json",
      x5u: "https://evil.test/cert.pem",
      jwk: { kty: "RSA", kid: "forged" },
    });
    const response = await getProjection(projectionRequest(bearer(token)), {
      params: Promise.resolve({ path: ["v1", "health"] }),
    });
    expect(response.status).toBe(200);
    expect(fetchedUrls).toEqual(["https://jwks.test/jwks.json"]);
  });

  it("JWK 的 use/alg/kty 不匹配时拒绝", async () => {
    jwksImpl = async () => ({
      keys: [{ ...KEY_A.publicJwk, use: "enc" }],
    });
    const response = await getProjection(
      projectionRequest(bearer(signJwt(KEY_A, {}))),
      { params: Promise.resolve({ path: ["v1", "health"] }) },
    );
    expect(response.status).toBe(401);
  });

  it("生产 JWKS 必须是 HTTPS 且主机在允许列表", async () => {
    const token = signJwt(KEY_A, {});
    applyEnv({ DSH_IAP_JWKS_URL: "http://jwks.test/jwks.json" });
    const httpUrl = await getProjection(projectionRequest(bearer(token)), {
      params: Promise.resolve({ path: ["v1", "health"] }),
    });
    expect(httpUrl.status).toBe(503);
    expect(jwksCalls).toBe(0);

    applyEnv({ DSH_IAP_JWKS_ALLOWED_HOSTS: "other.test" });
    const badHost = await getProjection(projectionRequest(bearer(token)), {
      params: Promise.resolve({ path: ["v1", "health"] }),
    });
    expect(badHost.status).toBe(503);
    expect(jwksCalls).toBe(0);
  });
});

// ---- ADR-003 完整映射(v2):审批/风控/评审/身份管理 ----

describe("rolesFromGroups (ADR-003 v2)", () => {
  it("maps all five groups per ADR", () => {
    expect(rolesFromGroups(["dsh-viewers"])).toEqual(["Viewer"]);
    expect(new Set(rolesFromGroups(["dsh-approvers"]))).toEqual(
      new Set(["Viewer", "Approver"]));
    expect(new Set(rolesFromGroups(["dsh-risk-operators"]))).toEqual(
      new Set(["Viewer", "RiskOperator"]));
    expect(new Set(rolesFromGroups(["dsh-strategy-reviewers"]))).toEqual(
      new Set(["Viewer", "StrategyReviewer"]));
    expect(rolesFromGroups(["dsh-identity-admins"])).toEqual(["IdentityAdmin"]);
  });

  it("production approvals are no longer a dead path", () => {
    const roles = rolesFromGroups(["dsh-approvers"]);
    expect(roles).toContain("Approver");
  });

  it("IdentityAdmin has no trading roles (separation of duties)", () => {
    const roles = rolesFromGroups(["dsh-identity-admins"]);
    expect(roles).not.toContain("Approver");
    expect(roles).not.toContain("RiskOperator");
  });

  it("unknown groups are ignored, roles are unioned", () => {
    expect(rolesFromGroups(["random-sso-group"])).toEqual([]);
    const union = new Set(rolesFromGroups(["dsh-approvers", "dsh-risk-operators"]));
    expect(union).toEqual(new Set(["Viewer", "Approver", "RiskOperator"]));
  });
});
