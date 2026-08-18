export const PROJECT_ROLES = [
  "Viewer",
  "Approver",
  "RiskOperator",
  "StrategyReviewer",
  "IdentityAdmin",
] as const;

export type ProjectRole = (typeof PROJECT_ROLES)[number];

export type AuthenticationMethod = "iap_jwt" | "development_mock";

/** 稳定主键是 issuer + subject_id，不用邮箱。 */
export type Principal = {
  subject_id: string;
  issuer: string;
  audience: string;
  roles: ProjectRole[];
  session_id?: string;
  auth_time?: number;
  expires_at: number;
  authentication_method: AuthenticationMethod;
  assurance_level?: string;
};

export type Jwk = {
  kid?: string;
  kty: string;
  alg?: string;
  use?: string;
  n?: string;
  e?: string;
  crv?: string;
  x?: string;
  y?: string;
};

export type JwksDocument = { keys: Jwk[] };

export const FORGED_IDENTITY_HEADERS = [
  "x-user",
  "x-forwarded-user",
  "x-forwarded-email",
  "x-forwarded-preferred-username",
  "x-remote-user",
  "x-actor",
  "x-actor-id",
  "decided_by",
  "actor_id",
] as const;

export const ALLOWED_IAP_ALGS = ["RS256", "ES256"] as const;
export const IAP_CLOCK_SKEW_SECONDS = 60;
export const IAP_JWKS_CACHE_TTL_SECONDS = 600;
