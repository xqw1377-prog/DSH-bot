import type { ProjectRole } from "./types";

/**
 * ADR-003 机器可读映射(完整版)。
 * v1 只认 Viewer,导致生产环境 Approver/RiskOperator 无组可映射,
 * 审批与紧急停止成为死路径——与页面的能力展示自相矛盾。
 * v2 按 ADR 建议落地全部五组;IdentityAdmin 刻意不含交易角色
 * (职责分离,见 ADR「Security Invariants」)。未知组仍忽略,不自动升格。
 */
export const GROUP_ROLE_MAPPING = {
  version: 2,
  groups: {
    "dsh-viewers": ["Viewer"],
    "dsh-approvers": ["Viewer", "Approver"],
    "dsh-risk-operators": ["Viewer", "RiskOperator"],
    "dsh-strategy-reviewers": ["Viewer", "StrategyReviewer"],
    "dsh-identity-admins": ["IdentityAdmin"],
  } satisfies Record<string, ProjectRole[]>,
} as const;

export function rolesFromGroups(groups: string[]): ProjectRole[] {
  const roles = new Set<ProjectRole>();
  for (const group of groups) {
    const mapped = GROUP_ROLE_MAPPING.groups[group as keyof typeof GROUP_ROLE_MAPPING.groups];
    if (!mapped) continue;
    for (const role of mapped) roles.add(role);
  }
  return [...roles];
}

export function groupsFromClaims(payload: Record<string, unknown>): string[] {
  const raw = payload.groups ?? payload["https://dsh.bot/groups"];
  if (Array.isArray(raw)) {
    return raw.filter((item): item is string => typeof item === "string");
  }
  if (typeof raw === "string") {
    return raw.split(/[,\s]+/).map((item) => item.trim()).filter(Boolean);
  }
  return [];
}
