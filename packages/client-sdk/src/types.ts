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

export type ApprovalStatus = "REQUESTED" | "APPROVED" | "REJECTED" | "EXPIRED";

export interface HealthStatus {
  market: Market;
  system_ok: boolean;
  data_fresh: boolean;
  trading_channel_ok: boolean;
  clock_skew_ms: number;
  degraded: boolean;
  detail?: string | null;
  as_of: string;
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
}
