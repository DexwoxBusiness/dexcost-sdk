// Canonical public-gross admission only; no monetary rates belong in the SDK.
/* eslint-disable @typescript-eslint/no-explicit-any */
export function isDirectStandardAnthropic(resource: any, body: any): boolean {
  try {
    const endpoint = new URL(String(resource?._client?.baseURL ?? resource?._client?.base_url ?? ""));
    return endpoint.protocol === "https:" && endpoint.hostname === "api.anthropic.com" &&
      [undefined, null, "standard"].includes(body?.speed) &&
      [undefined, null, "global"].includes(body?.inference_geo) && !body?.betas?.length;
  } catch { return false; }
}

export function directAnthropicUsage(usage: any, stopReason: unknown, direct: boolean) {
  const count = (value: unknown) => typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
  const input = usage?.input_tokens;
  const output = usage?.output_tokens;
  const read = usage?.cache_read_input_tokens ?? 0;
  const write = usage?.cache_creation_input_tokens ?? 0;
  const oneHour = usage?.cache_creation?.ephemeral_1h_input_tokens ?? 0;
  const fiveMinute = usage?.cache_creation?.ephemeral_5m_input_tokens ?? 0;
  const thinking = usage?.output_tokens_details?.thinking_tokens ?? 0;
  const valid = [input, output, read, write, oneHour, fiveMinute, thinking].every(count) &&
    thinking <= output && oneHour <= write &&
    (write === 0 || (usage?.cache_creation?.ephemeral_1h_input_tokens != null && usage?.cache_creation?.ephemeral_5m_input_tokens != null)) &&
    (usage?.cache_creation == null || oneHour + fiveMinute === write);
  const lines = valid ? [
    ["input_tokens", input], ["output_tokens", output - thinking],
    ["reasoning_output_tokens", thinking], ["cache_read_input_tokens", read],
    ["cache_write_input_tokens", write - oneHour], ["cache_write_input_tokens_1h", oneHour],
  ].filter(([, quantity]) => Number(quantity) > 0).map(([metric, quantity]) => ({
    metric: String(metric), quantity: String(quantity), unit: "Tokens",
  })) : undefined;
  const admitted = direct && valid && usage?.service_tier === "standard" &&
    [undefined, null, "global"].includes(usage?.inference_geo) &&
    [undefined, null, "standard"].includes(usage?.speed) &&
    !usage?.iterations?.length && usage?.fallback_credit == null &&
    usage?.server_tool_use == null && ["end_turn", "max_tokens", "stop_sequence", "tool_use"].includes(String(stopReason));
  return { lines, dimensions: admitted ? [{ key: "direct_llm_pricing_lane", value: {
    type: "string", value: "standard_global",
  } }] : [] };
}
