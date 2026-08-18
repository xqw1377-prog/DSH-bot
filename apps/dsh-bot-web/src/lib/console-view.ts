import type { BotOverview, BotsOverview } from "@dsh-bot/client-sdk";
import type { Principal, ProjectRole } from "@/lib/identity";

export type ConsoleCapabilities = {
  canDecide: boolean;
  canEmergencyStop: boolean;
};

export function capabilitiesFrom(principal: Principal): ConsoleCapabilities {
  return {
    canDecide: principal.roles.includes("Approver"),
    canEmergencyStop: principal.roles.includes("RiskOperator"),
  };
}

export function hasRole(principal: Principal, role: ProjectRole): boolean {
  return principal.roles.includes(role);
}

export function botById(
  overview: BotsOverview,
  botId: BotOverview["bot_id"],
): BotOverview | undefined {
  return overview.bots.find((bot) => bot.bot_id === botId);
}

export function dataLooksHealthy(data: BotOverview["data"]): boolean {
  return data === "FRESH" || data === "MARKET_CLOSED";
}

export function writeActionProps(
  enabled: boolean,
  onAct: () => void,
): { disabled: true } | { disabled: false; onClick: () => void } {
  if (!enabled) {
    return { disabled: true };
  }
  return { disabled: false, onClick: onAct };
}

export async function afterViewer<T>(
  viewer: () => Promise<{ principal: Principal } | { error: { status: number } }>,
  load: () => Promise<T>,
): Promise<
  | { principal: Principal; data: T }
  | { error: { status: number } }
> {
  const auth = await viewer();
  if ("error" in auth) {
    return { error: auth.error };
  }
  return { principal: auth.principal, data: await load() };
}

export function homepageAlerts(overview: BotsOverview): string[] {
  return overview.alerts;
}

export function hasLiveSelector(markup: string): boolean {
  return /<select[\s\S]*LIVE[\s\S]*<\/select>|<option[^>]*>\s*LIVE/i.test(markup);
}
