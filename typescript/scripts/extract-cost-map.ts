import { readFileSync, writeFileSync } from "node:fs";

const full = JSON.parse(
  readFileSync("../../sdks/python/src/dexcost/data/model_cost_map.json", "utf-8")
);

const chat: Record<string, { input_cost_per_token: number; output_cost_per_token: number; cache_read_input_token_cost?: number; cache_creation_input_token_cost?: number }> = {};

for (const [key, val] of Object.entries(full) as [string, any][]) {
  if (key === "sample_spec") continue;
  if (val.mode !== "chat") continue;
  if (val.input_cost_per_token == null || val.output_cost_per_token == null) continue;
  const entry: any = {
    input_cost_per_token: val.input_cost_per_token,
    output_cost_per_token: val.output_cost_per_token,
  };
  if (val.cache_read_input_token_cost) entry.cache_read_input_token_cost = val.cache_read_input_token_cost;
  if (val.cache_creation_input_token_cost) entry.cache_creation_input_token_cost = val.cache_creation_input_token_cost;
  chat[key] = entry;
}

writeFileSync(
  "src/pricing/cost_map.json",
  JSON.stringify(chat, null, 2),
  "utf-8"
);
console.log(`Extracted ${Object.keys(chat).length} chat models`);
