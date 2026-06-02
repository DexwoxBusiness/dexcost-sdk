/** Hardcoded default Control Layer endpoint. */
export const DEFAULT_ENDPOINT = "https://api.dexcost.io";

/**
 * Resolve the Control Layer endpoint from the DEXCOST_ENDPOINT env var. Only
 * https:// URLs are accepted: an attacker who controls the env (misconfigured
 * CI runner, hostile container) could otherwise silently exfiltrate cost
 * telemetry AND the Bearer API key to an HTTP collector. We refuse and fall
 * back to the production default with a console.warn.
 *
 * Exported for testability and shared by both the tracker (pricing refresh)
 * and the event pusher (telemetry POST) so neither path can bypass it.
 */
export function resolveEndpoint(): string {
  const env = process.env.DEXCOST_ENDPOINT;
  if (env === undefined || env === "") {
    return DEFAULT_ENDPOINT;
  }
  if (!env.startsWith("https://")) {
    console.warn(
      `dexcost: DEXCOST_ENDPOINT=${JSON.stringify(env)} rejected — only ` +
        `https:// URLs are accepted. Falling back to ${DEFAULT_ENDPOINT}.`,
    );
    return DEFAULT_ENDPOINT;
  }
  return env;
}
