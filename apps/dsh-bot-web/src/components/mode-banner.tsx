export function ModeBanner({
  globalMode,
  liveAnomaly,
}: {
  globalMode: "PAPER" | "SHADOW" | "MIXED";
  liveAnomaly: boolean;
}) {
  return (
    <div
      data-testid="global-mode"
      style={{
        marginLeft: "auto",
        display: "flex",
        alignItems: "center",
        gap: 12,
        fontSize: 13,
        fontWeight: 600,
      }}
    >
      <span>GLOBAL MODE: {globalMode}</span>
      {liveAnomaly && (
        <span data-testid="live-anomaly" style={{ color: "#b91c1c" }}>
          LIVE 异常
        </span>
      )}
    </div>
  );
}
