export function isDevelopmentEnv(): boolean {
  return process.env.DSH_ENV === "development";
}
