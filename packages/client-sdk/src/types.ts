// 领域类型：与 packages/domain-contracts 的 Pydantic 模型保持一致。
// 数量与金额使用 string 传输（decimal string），避免浮点误差。

export type Market = "A_SHARE" | "CRYPTO";
export type OrderSide = "BUY" | "SELL";

export type OrderStatus =
  | "INTENT_CREATED"
  | "RISK_PASSED"
  | "APPROVAL_PENDING"
  | "APPROVED"
  | "SUBMITTED"
  | "ACKNOWLEDGED"
  | "PARTIALLY_FILLED"
  | "FILLED"
  | "CANCELLED"
  | "REJECTED"
  | "UNKNOWN"
  | "RECONCILING"
  | "FAILED";

export type StrategyStage =
  | "DRAFT"
  | "BACKTESTED"
  | "VALIDATED"
  | "PAPER"
  | "SHADOW"
  | "APPROVED"
  | "CANARY"
  | "PRODUCTION"
  | "RETIRED"
  | "ROLLED_BACK";

/**
 * 审批状态机:REQUESTED → APPROVED/REJECTED(人工决定);
 * APPROVED → CONSUMING(被某笔订单原子占用) → CONSUMED(成交回填)。
 * 一个审批只能被消费一次;EXPIRED 为超时终态。
 */
export type ApprovalStatus =
  | "REQUESTED"
  | "APPROVED"
  | "REJECTED"
  | "EXPIRED"
  | "CONSUMING"
  | "CONSUMED";

/** Bot 任务状态机，见 PRD 附录 A.3。 */
export type TaskStatus =
  | "QUEUED"
  | "RUNNING"
  | "WAITING_FOR_TOOL"
  | "WAITING_FOR_APPROVAL"
  | "COMPLETED"
  | "FAILED"
  | "CANCELLED";

export interface HealthStatus {
  market: Market;
  system_ok: boolean;
  data_fresh: boolean;
  trading_channel_ok: boolean;
  clock_skew_ms: number;
  degraded: boolean;
  detail?: string | null;
  as_of: string;
  source_system?: string | null;
  source_mode?: string | null;
  source_observed_at?: string | null;
  snapshot_id?: string | null;
  market_session?: "OPEN" | "CLOSED" | "UNKNOWN" | null;
}

export interface Position {
  market: Market;
  account_id: string;
  symbol: string;
  quantity: string;
  available_quantity: string;
  frozen_quantity: string;
  avg_cost: string;
  currency: string;
  as_of: string;
}

export interface AccountSummary {
  market: Market;
  account_id: string;
  cash: string;
  equity: string;
  margin_used?: string | null;
  available_cash?: string | null;
  frozen_cash?: string | null;
  currency: string;
  reconciliation_version: string;
  as_of: string;
}

export interface Signal {
  signal_id: string;
  market: Market;
  strategy_id: string;
  strategy_version: string;
  symbol: string;
  side: OrderSide;
  strength?: number | null;
  generated_at: string;
  valid_until: string;
  data_snapshot_id: string;
  evidence_refs?: string[] | null;
  quantity?: string | null;
  entry_price?: string | null;
  source_action?: string | null;
  why_source?: string[] | null;
}

export type ShadowAction = "BUY" | "SELL" | "HOLD" | "ABANDON";

export interface ShadowDecision {
  task_id: string;
  bot: string;
  signal_id?: string | null;
  market?: Market | string | null;
  symbol?: string | null;
  side?: OrderSide | string | null;
  status: string;
  action?: ShadowAction | string;
  quantity?: string | null;
  suggested_price?: string | null;
  price_low?: string | null;
  price_high?: string | null;
  strategy_version?: string | null;
  strength?: number | null;
  position_after?: string | null;
  worst_case_loss?: string | null;
  primary_risks?: string[];
  why?: string | null;
  why_not?: string | null;
  skip_reason?: string | null;
  valid_until?: string | null;
  evidence_refs?: string[];
  simulation_only?: boolean;
  disclaimer?: string;
  outcome_price?: string | null;
  outcome_at?: string | null;
  simulated_pnl?: string | null;
  updated_at?: string;
}

export type TodayStory = {
  market: string;
  title: string;
  points: string[];
};

export interface TodayBoard {
  headline: string;
  stories: TodayStory[];
  focus: ShadowDecision[];
  abandons: ShadowDecision[];
  watching: Array<Record<string, unknown>>;
  screens: Array<Record<string, unknown>>;
  attention?: Array<Record<string, unknown>>;
  counts: Record<string, number>;
  disclaimer: string;
}

export type QualitySuggestion = {
  suggestion_id: string;
  title: string;
  reason: string;
  evidence: string[];
  stage: "SUGGESTION";
  next_stage: "REPLAY";
  can_apply: false;
  pipeline: string;
};

export interface IntelligenceReport {
  as_of?: string | null;
  mode: "SHADOW";
  disclaimer: string;
  documents: Array<Record<string, unknown>>;
  events: Array<Record<string, unknown>>;
  coverage?: Record<string, boolean>;
}

export interface IntelligenceItem {
  item_id: string;
  bot: string;
  market: string;
  source_id: string;
  symbol?: string | null;
  title: string;
  source_url?: string | null;
  published_at?: string | null;
  observed_at: string;
  authority?: string | null;
  direction?: string | null;
  horizon?: string | null;
  importance?: number | null;
  confidence?: number | null;
  action?: string | null;
  payload: Record<string, unknown>;
}

export interface AuditReportRecord {
  report_id: string;
  bot: string;
  market: string;
  report_kind: string;
  period_key: string;
  created_at: string;
  payload: Record<string, unknown>;
}

export interface TradeQualityReport {
  as_of?: string | null;
  disclaimer: string;
  pipeline: string[];
  coverage: Record<string, boolean>;
  score: {
    overall: number | null;
    dimensions: Record<string, { score: number | null; available: boolean; note?: string }>;
  };
  counts: Record<string, number>;
  worst: Array<Record<string, unknown>>;
  best: Array<Record<string, unknown>>;
  exit_notes: string[];
  size_notes: string[];
  suggestions: QualitySuggestion[];
}

export interface ChiefBriefing {
  as_of?: string | null;
  focus: ShadowDecision[];
  risks: ShadowDecision[];
  abandons: ShadowDecision[];
  ranked?: ShadowDecision[];
  counts: Record<string, number>;
  text?: string;
}

export interface RiskSnapshot {
  risk_snapshot_id: string;
  market: Market;
  account_id: string;
  position_before: string;
  position_after: string;
  risk_budget_delta: string;
  worst_case_loss: string;
  limits_hit: string[];
  as_of: string;
}

export interface OrderIntent {
  idempotency_key: string;
  market: Market;
  account_id: string;
  strategy_id: string;
  strategy_version: string;
  symbol: string;
  side: OrderSide;
  quantity: string;
  valid_until: string;
  signal_snapshot_id: string;
  risk_snapshot_id: string;
  approval_id?: string | null;
}

export interface OrderPreview {
  intent: OrderIntent;
  estimated_cost: string;
  estimated_slippage: string;
  risk: RiskSnapshot;
}

export interface Approval {
  approval_id: string;
  status: ApprovalStatus;
  market: Market;
  requested_by_bot: string;
  requested_at: string;
  decided_by?: string | null;
  decided_at?: string | null;
  subject_type: "order" | "strategy_promotion" | "risk_budget" | "control_action";
  subject_id: string;
  evidence_refs: string[];
  /** 订单审批的意图绑定摘要(SHA-256);strategy_promotion 无绑定 */
  intent_digest?: string | null;
  /** 审批有效期;过期未决/未消费即 EXPIRED */
  expires_at?: string | null;
  /** 消费痕迹:CONSUMING/CONSUMED 时非空,回填成交后含权威订单 ID */
  consumed_key?: string | null;
  consumed_request_hash?: string | null;
  consumed_order_id?: string | null;
  consumed_at?: string | null;
}

/** 研究实验账本条目，见 PRD 10.4。实验环境无生产密钥。 */
export interface Experiment {
  experiment_id: string;
  market: Market;
  strategy_id: string;
  hypothesis: string;
  data_snapshot_id: string;
  status: TaskStatus;
  created_by_bot: string;
  created_at: string;
  result_ref?: string | null;
}

/** 策略晋级状态机条目，见 PRD 附录 A.1。单次回测不足以晋级：stage 推进必须附带证据引用。 */
export interface StrategyCandidate {
  candidate_id: string;
  market: Market;
  strategy_id: string;
  strategy_version: string;
  stage: StrategyStage;
  experiment_id?: string | null;
  evidence_refs: string[];
  approval_id?: string | null;
  updated_at: string;
}
