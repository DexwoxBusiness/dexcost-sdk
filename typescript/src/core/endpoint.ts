/** Hardcoded default Control Layer endpoint. */
export const DEFAULT_ENDPOINT = "https://api.dexcost.io";

/**
 * Resolve the Control Layer endpoint from explicit in-code configuration.
 *
 * The endpoint is NEVER read from the process environment: an attacker who
 * controls the env (misconfigured CI runner, hostile container) could otherwise
 * set `DEXCOST_ENDPOINT=http://attacker/` and silently exfiltrate cost telemetry
 * AND the Bearer API key to an HTTP collector. By sourcing the endpoint only
 * from the explicit `TrackerOptions.endpoint` option (or the hardcoded
 * production default), that vector is closed entirely.
 *
 * The explicit value is developer-supplied/trusted, so validation is minimal:
 * if it does not start with `http://` or `https://`, we `console.warn` and fall
 * back to the default. `http://` is intentionally allowed here (e.g.
 * `http://localhost` for e2e) precisely because it is not env-controllable.
 *
 * Exported for testability and shared by both the tracker (pricing refresh)
 * and the event pusher (telemetry POST) so neither path can bypass it.
 */
export function resolveEndpoint(explicit?: string): string {
  if (explicit === undefined || explicit === "") {
    return DEFAULT_ENDPOINT;
  }
  if (!explicit.startsWith("http://") && !explicit.startsWith("https://")) {
    console.warn(
      `dexcost: endpoint=${JSON.stringify(explicit)} rejected — must start ` +
        `with http:// or https://. Falling back to ${DEFAULT_ENDPOINT}.`,
    );
    return DEFAULT_ENDPOINT;
  }
  return explicit;
}
