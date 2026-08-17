import type {
  AccountSummary,
  Approval,
  ApprovalStatus,
  Experiment,
  HealthStatus,
  Market,
  Position,
  Signal,
  StrategyCandidate,
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

  /** 审批列表，可按状态过滤（如 status=REQUESTED）。 */
  getApprovals(status?: ApprovalStatus): Promise<Approval[]> {
    const query = status ? `?status=${status}` : "";
    return this.get(`/v1/approvals${query}`);
  }

  /** 研究实验列表，可按市场过滤。 */
  getExperiments(market?: Market): Promise<Experiment[]> {
    const query = market ? `?market=${market}` : "";
    return this.get(`/v1/experiments${query}`);
  }

  /** 策略候选列表，可按市场过滤。 */
  getCandidates(market?: Market): Promise<StrategyCandidate[]> {
    const query = market ? `?market=${market}` : "";
    return this.get(`/v1/candidates${query}`);
  }
}

/**
 * 审批动作客户端（写操作）。
 *
 * 审批决定是改变资金状态的动作，projection-api 是只读的，所以决定必须
 * 提交到 Quant Gateway。本类把 fetch 细节封装进 SDK，前端组件不再裸调
 * 网关地址，保证分层与可测试性。
 */
export class ApprovalActionsClient {
  constructor(
    private readonly gatewayUrl: string,
    private readonly fetchImpl: typeof fetch = fetch,
  ) {}

  /** 提交审批决定。decision ∈ { APPROVED, REJECTED }。 */
  async decide(
    approvalId: string,
    decision: "APPROVED" | "REJECTED",
    decidedBy: string,
  ): Promise<Approval> {
    const res = await this.fetchImpl(
      `${this.gatewayUrl}/v1/approvals/${approvalId}/decide`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ decision, decided_by: decidedBy }),
      },
    );
    if (!res.ok) {
      throw new Error(`decide ${approvalId} failed: ${res.status}`);
    }
    return (await res.json()) as Approval;
  }
}
