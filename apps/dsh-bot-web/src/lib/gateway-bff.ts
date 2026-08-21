export function gatewayUrl(): string {
  return process.env.QUANT_GATEWAY_URL || "http://127.0.0.1:8001";
}

export function gatewayHeaders(
  extra: Record<string, string> = {},
): Record<string, string> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...extra,
  };
  const apiKey = process.env.QUANT_GATEWAY_API_KEY || "";
  if (apiKey) {
    headers["X-API-Key"] = apiKey;
  }
  return headers;
}
