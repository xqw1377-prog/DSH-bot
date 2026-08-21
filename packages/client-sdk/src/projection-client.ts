import type {
  AccountSummary,
  Approval,
  ApprovalStatus,
  ChiefBriefing,
  Experiment,
  HealthStatus,
  Market,
  Position,
  ShadowDecision,
  Signal,
  StrategyCandidate,
  TodayBoard,
  IntelligenceReport,
  IntelligenceItem,
  AuditReportRecord,
  TradeQualityReport,
} from "./types.js";

export type ProjectionClientOptions = {
  headers?: Record<string, string> | (() => Record<string, string>);
};

/** 面向前端的只读投影客户端。资金动作不经过此客户端，必须走审批流程。 */
export class ProjectionClient {
  constructor(
    private readonly baseUrl: string,
    private readonly options: ProjectionClientOptions = {},
  ) {}

  private requestHeaders(extra: Record<string, string> = {}): Record<string, string> {
    const provided =
      typeof this.options.headers === "function"
        ? this.options.headers()
        : this.options.headers ?? {};
    return { ...provided, ...extra };
  }

  private async get<T>(path: string): Promise<T> {
    const res = await fetch(`${this.baseUrl}${path}`, {
      cache: "no-store",
      headers: this.requestHeaders(),
    });
    if (!res.ok) {
      throw new Error(`projection-api ${path} failed: ${res.status}`);
    }
    return (await res.json()) as T;
  }

  private async post<T>(path: string, body: unknown): Promise<T> {
    const res = await fetch(`${this.baseUrl}${path}`, {
      method: "POST",
      cache: "no-store",
      headers: this.requestHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify(body),
    });
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

  getIncidents(limit?: number): Promise<IncidentEvent[]> {
    const query = limit ? `?limit=${limit}` : "";
    return this.get(`/v1/incidents${query}`);
  }

  queryChief(question: string): Promise<ChiefAnswer> {
    return this.post("/v1/chief/query", { question });
  }

  getBotTasks(bot?: string, status?: string): Promise<BotTask[]> {
    const params = new URLSearchParams();
    if (bot) params.set("bot", bot);
    if (status) params.set("status", status);
    const query = params.toString();
    return this.get(`/v1/bot-tasks${query ? `?${query}` : ""}`);
  }

  getBotsOverview(): Promise<BotsOverview> {
    return this.get("/v1/bots/overview");
  }

  getShadowDecisions(bot?: string): Promise<ShadowDecision[]> {
    const query = bot ? `?bot=${encodeURIComponent(bot)}` : "";
    return this.get(`/v1/shadow-decisions${query}`);
  }

  getChiefBriefing(): Promise<ChiefBriefing> {
    return this.get("/v1/chief/briefing");
  }

  getToday(): Promise<TodayBoard> {
    return this.get("/v1/today");
  }

  getTradeQuality(): Promise<TradeQualityReport> {
    return this.get("/v1/trade-quality");
  }

  getIntelligence(): Promise<IntelligenceReport> {
    return this.get("/v1/intelligence");
  }

  getIntelligenceFeed(bot?: string, market?: Market, limit?: number): Promise<IntelligenceItem[]> {
    const params = new URLSearchParams();
    if (bot) params.set("bot", bot);
    if (market) params.set("market", market);
    if (limit) params.set("limit", String(limit));
    const query = params.toString();
    return this.get(`/v1/intelligence/feed${query ? `?${query}` : ""}`);
  }

  getAuditReports(bot?: string, reportKind?: string, limit?: number): Promise<AuditReportRecord[]> {
    const params = new URLSearchParams();
    if (bot) params.set("bot", bot);
    if (reportKind) params.set("report_kind", reportKind);
    if (limit) params.set("limit", String(limit));
    const query = params.toString();
    return this.get(`/v1/audit/reports${query ? `?${query}` : ""}`);
  }
}

export type BotRuntime = "ONLINE" | "DEGRADED" | "OFFLINE";
export type BotMode = "PAPER" | "SHADOW" | "LIVE" | "MIXED" | "UNKNOWN";
export type BotData = "FRESH" | "MARKET_CLOSED" | "STALE" | "DISCONNECTED";
export type GlobalMode =
  | "PAPER"
  | "SHADOW"
  | "MIXED"
  | "UNKNOWN"
  | "SECURITY_VIOLATION";
export type BotSeverity =
  | "HALTED"
  | "INCIDENT"
  | "UNKNOWN"
  | "DEGRADED"
  | "WARNING"
  | "NORMAL";
export type BotTaskDim =
  | "IDLE"
  | "ANALYZING"
  | "AWAITING_APPROVAL"
  | "EXECUTING"
  | "RECONCILING";
export type BotOrderDim = "NONE" | "OPEN" | "PARTIAL" | "UNKNOWN" | "REJECTED";
export type BotRisk = "NORMAL" | "WARNING" | "INCIDENT" | "HALTED";

export type BotOverview = {
  bot_id: "market-chief" | "crypto" | "a-share";
  label: string;
  market: Market | null;
  read_only: boolean;
  as_of: string;
  runtime: BotRuntime;
  mode: BotMode;
  data: BotData;
  task: BotTaskDim;
  order: BotOrderDim;
  risk: BotRisk;
  severity?: BotSeverity;
  clock_skew_ms: number | null;
  degraded: boolean;
  detail?: string | null;
  source_system?: string | null;
  source_mode?: string | null;
  source_observed_at?: string | null;
  exported_at?: string | null;
  snapshot_age_seconds?: number | null;
  export_age_seconds?: number | null;
  connection: "CONNECTED" | "DISCONNECTED";
  counts: {
    pending_approvals: number;
    open_orders: number;
    unknown_orders: number;
    incidents: number;
  };
};

export type BotsOverview = {
  as_of: string;
  global_mode: GlobalMode;
  live_anomaly: boolean;
  alerts: string[];
  bots: BotOverview[];
};

export type IncidentEvent = {
  event_id: string;
  event_type: string;
  occurred_at: string;
  market: string;
  actor: { kind: string; id: string };
  payload: Record<string, unknown>;
};

export type ChiefAnswer = {
  role: string;
  refused: boolean;
  text: string;
};

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
