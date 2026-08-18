import type { GlobalMode } from "@dsh-bot/client-sdk";

export function ModeBanner({
  globalMode,
  liveAnomaly,
}: {
  globalMode: GlobalMode;
  liveAnomaly: boolean;
}) {
  const violation = globalMode === "SECURITY_VIOLATION" || liveAnomaly;
  const label = violation ? "SECURITY VIOLATION" : globalMode;
  return (
    <div
      data-testid="global-mode"
      style={{
        marginLeft: "auto",
        display: "flex",
        alignItems: "center",
        gap: 12,
        fontSize: 13,
        fontWeight: 700,
        color: violation ? "#991b1b" : "#111827",
        backgroundColor: violation ? "#fef2f2" : "transparent",
        border: violation ? "1px solid #fecaca" : "none",
        borderRadius: 6,
        padding: violation ? "6px 10px" : 0,
      }}
    >
      <span>GLOBAL MODE: {label}</span>
    </div>
  );
}
