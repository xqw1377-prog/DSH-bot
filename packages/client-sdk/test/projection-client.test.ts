import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { ProjectionClient, ApprovalActionsClient } from "../src/index.js";

describe("ProjectionClient", () => {
  it("getApprovals 透传 status 查询参数", async () => {
    let calledPath = "";
    const fakeFetch: typeof fetch = async (input) => {
      calledPath = input.toString();
      return new Response(JSON.stringify([]), { status: 200 });
    };
    const client = new ProjectionClient("http://api.test");
    // 临时替换全局 fetch
    const original = globalThis.fetch;
    globalThis.fetch = fakeFetch;
    try {
      const result = await client.getApprovals("REQUESTED");
      assert.equal(result.length, 0);
      assert.equal(calledPath, "http://api.test/v1/approvals?status=REQUESTED");
    } finally {
      globalThis.fetch = original;
    }
  });

  it("无 status 时不带查询参数", async () => {
    let calledPath = "";
    const fakeFetch: typeof fetch = async (input) => {
      calledPath = input.toString();
      return new Response(JSON.stringify([]), { status: 200 });
    };
    const client = new ProjectionClient("http://api.test");
    const original = globalThis.fetch;
    globalThis.fetch = fakeFetch;
    try {
      await client.getApprovals();
      assert.equal(calledPath, "http://api.test/v1/approvals");
    } finally {
      globalThis.fetch = original;
    }
  });

  it("HTTP 错误抛异常", async () => {
    const fakeFetch: typeof fetch = async () =>
      new Response("err", { status: 500 });
    const client = new ProjectionClient("http://api.test");
    const original = globalThis.fetch;
    globalThis.fetch = fakeFetch;
    try {
      await assert.rejects(() => client.getHealth("CRYPTO"));
    } finally {
      globalThis.fetch = original;
    }
  });
});

describe("ApprovalActionsClient", () => {
  it("decide POST 到正确端点并返回 Approval", async () => {
    let capturedBody = "";
    let capturedMethod = "";
    const fakeFetch: typeof fetch = async (_input, init) => {
      capturedMethod = init?.method ?? "";
      capturedBody = init?.body?.toString() ?? "";
      return new Response(
        JSON.stringify({ approval_id: "a1", status: "APPROVED" }),
        { status: 200 },
      );
    };
    const client = new ApprovalActionsClient("http://gw.test", fakeFetch);
    const result = await client.decide("a1", "APPROVED", "human");
    assert.equal(result.approval_id, "a1");
    assert.equal(result.status, "APPROVED");
    assert.equal(capturedMethod, "POST");
    assert.ok(capturedBody.includes('"decision":"APPROVED"'));
    assert.ok(capturedBody.includes('"decided_by":"human"'));
  });

  it("decide 失败抛异常", async () => {
    const fakeFetch: typeof fetch = async () =>
      new Response("err", { status: 422 });
    const client = new ApprovalActionsClient("http://gw.test", fakeFetch);
    await assert.rejects(() => client.decide("a1", "APPROVED", "human"));
  });
});
