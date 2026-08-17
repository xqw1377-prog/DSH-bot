import { describe, expect, it } from "vitest";
import { ProjectionClient } from "@dsh-bot/client-sdk";

describe("ProjectionClient", () => {
  it("按状态过滤审批（URL 拼接正确）", async () => {
    const requested: string[] = [];
    const client = new ProjectionClient("http://api.test");
    (client as unknown as {
      get: (p: string) => Promise<unknown[]>;
    }).get = async (p: string) => {
      requested.push(p);
      return [];
    };
    await client.getApprovals("REQUESTED");
    expect(requested).toEqual(["/v1/approvals?status=REQUESTED"]);
  });
});
