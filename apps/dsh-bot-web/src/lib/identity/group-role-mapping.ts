import type { ProjectRole } from "./types";

/**
 * ADR-003 机器可读映射。第一版只认 Viewer。
 * Approver / RiskOperator 等组名即使出现在 Token 里也不映射。
 */
export const GROUP_ROLE_MAPPING = {
  version: 1,
  groups: {
    "dsh-viewers": ["Viewer"],
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
