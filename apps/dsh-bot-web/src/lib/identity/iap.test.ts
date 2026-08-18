import { generateKeyPairSync, createSign, type KeyObject } from "node:crypto";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { POST as decideApproval } from "@/app/api/approvals/[id]/decide/route";
import { POST as emergencyStop } from "@/app/api/control/emergency-stop/route";
import { GET as getProjection } from "@/app/api/projection/[...path]/route";
import { resetJwksCacheForTests, setJwksTestHooks } from "@/lib/identity";
import type { Jwk } from "./types";

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
    exp: NOW_SEC + 300,
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
  DSH_IAP_JWKS_URL: "http://jwks.test/jwks.json",
};

const savedEnv = { ...process.env };
const realFetch = globalThis.fetch;
let gatewayCalls = 0;
let jwksCalls = 0;
let jwksImpl: () => Promise<{ keys: Jwk[] }> = async () => ({
  keys: [KEY_A.publicJwk],
});

function applyEnv(extra: Record<string, string | undefined> = {}): void {
  for (const key of Object.keys(process.env)) {
    if (
      key.startsWith("DSH_") ||
      key === "PROJECTION_API_URL" ||
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
  jwksImpl = async () => ({ keys: [KEY_A.publicJwk] });
  resetJwksCacheForTests();
  setJwksTestHooks({
    now: () => NOW_MS,
    fetcher: async () => {
      jwksCalls += 1;
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
    const noneAlg = unsignedJwt({ alg: "none", kid: "kid-a" }, {});

    for (const token of [wrongSig, wrongIss, wrongAud, expired, noneAlg]) {
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
    expect(jwksCalls).toBe(2);

    const unknown = await getProjection(
      projectionRequest(bearer(signJwt(KEY_B, {}))),
      { params: Promise.resolve({ path: ["v1", "health"] }) },
    );
    expect(unknown.status).toBe(401);
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
    const token = signJwt(KEY_A, { groups: ["dsh-approvers"] });
    const response = await getProjection(projectionRequest(bearer(token)), {
      params: Promise.resolve({ path: ["v1", "health"] }),
    });
    expect(response.status).toBe(403);
  });
});
