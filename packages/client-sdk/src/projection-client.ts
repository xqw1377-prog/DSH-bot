import type {
  AccountSummary,
  Approval,
  HealthStatus,
  Market,
  Position,
  Signal,
} from "./types.js";

/** 面向前端的只读投影客户端。资金动作不经过此客户端，必须走审批流程。 */
export class ProjectionClient {
  constructor(private readonly baseUrl: string) {}

  private async get<T>(path: string): Promise<T> {
    const res = await fetch(`${this.baseUrl}${path}`);
    if (!res.ok) {
      throw new Error(`projection-api ${path} failed: ${res.status}`);
    }
    return (await res.json()) as T;
  }

  getHealth(market: Market): Promise<HealthStatus> {
    return this.get(`/v1/markets/${market}/health`);
  }

  getPositions(market: Market): Promise<Position[]> {
    return this.get(`/v1/markets/${market}/positions`);
  }

  getAccountSummary(market: Market): Promise<AccountSummary[]> {
    return this.get(`/v1/markets/${market}/accounts`);
  }

  getSignals(market: Market): Promise<Signal[]> {
    return this.get(`/v1/markets/${market}/signals`);
  }

  getPendingApprovals(): Promise<Approval[]> {
    return this.get(`/v1/approvals?status=REQUESTED`);
  }
}
