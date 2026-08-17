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

  getBotTasks(bot?: string, status?: string): Promise<BotTask[]> {
    const params = new URLSearchParams();
    if (bot) params.set("bot", bot);
    if (status) params.set("status", status);
    const query = params.toString();
    return this.get(`/v1/bot-tasks${query ? `?${query}` : ""}`);
  }
}

export type BotTask = {
  task_id: string;
  bot: string;
  kind: string;
  status: string;
  subject_id: string;
  approval_id?: string | null;
  order_id?: string | null;
  reconciliation_status: string;
  payload: Record<string, unknown>;
  updated_at: string;
};
