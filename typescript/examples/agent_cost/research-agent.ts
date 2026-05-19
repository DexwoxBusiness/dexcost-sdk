/**
 * Dexcost E2E Research Assistant Agent
 * Real cost-attribution scenario:
 *   - Voyage AI embeddings + rerank        -> non-LLM external_cost
 *   - MiniMax M2.7 (Anthropic-compatible)  -> llm_call
 *   - simulated 429 retry                  -> retry_marker
 *
 * Run (inside the sandbox):
 *   cd sdks/typescript
 *   export MINIMAX_API_KEY=... VOYAGE_API_KEY=...
 *   export DEXCOST_ENDPOINT=http://localhost:3000
 *   export DEXCOST_API_KEY=dx_live_...
 *   npx tsx examples/agent_cost/research-agent.ts
 */

import { init, closeAsync, getTracker } from "../../src/index.js";

// ---------------------------------------------------------------------------
// Config from environment
// ---------------------------------------------------------------------------

const MINIMAX_API_KEY = process.env.MINIMAX_API_KEY ?? "";
const VOYAGE_API_KEY = process.env.VOYAGE_API_KEY ?? "";
const ANTHROPIC_BASE_URL = process.env.ANTHROPIC_BASE_URL ?? "https://api.minimax.io/anthropic";
const ANTHROPIC_MODEL = process.env.ANTHROPIC_MODEL ?? "MiniMax-M2.7";
const MINIMAX_ENDPOINT = `${ANTHROPIC_BASE_URL}/v1/messages`;
const VOYAGE_EMBED_ENDPOINT = "https://api.voyageai.com/v1/embeddings";
const VOYAGE_RERANK_ENDPOINT = "https://api.voyageai.com/v1/rerank";
const VOYAGE_EMBED_MODEL = process.env.VOYAGE_EMBED_MODEL ?? "voyage-3-large";
const VOYAGE_RERANK_MODEL = process.env.VOYAGE_RERANK_MODEL ?? "rerank-2.5";

// Public Voyage list pricing (per 1M tokens / per 1k searches) — sept 2025
const VOYAGE_EMBED_USD_PER_1M_TOKENS = 0.18; // voyage-3-large
const VOYAGE_RERANK_USD_PER_1K_SEARCHES = 0.05; // rerank-2.5

// MiniMax M2.7 pricing (input $0.30 / output $1.20 per 1M)
const MINIMAX_INPUT_USD_PER_1M = 0.30;
const MINIMAX_OUTPUT_USD_PER_1M = 1.20;

// dexcost SDK config — set DEXCOST_ENDPOINT + DEXCOST_API_KEY to push events
const DEXCOST_API_URL = process.env.DEXCOST_ENDPOINT ?? process.env.DEXCOST_API_URL ?? "http://localhost:3000";
const DEXCOST_API_KEY = process.env.DEXCOST_API_KEY ?? "";

// ---------------------------------------------------------------------------
// Voyage AI: real embeddings (HTTP)
// ---------------------------------------------------------------------------

interface VoyageEmbeddingResponse {
  object: string;
  data: Array<{ object: string; embedding: number[]; index: number }>;
  model: string;
  usage: { total_tokens: number };
}

async function embedDocuments(documents: string[]): Promise<number> {
  console.log(`[agent] Embedding ${documents.length} docs with Voyage ${VOYAGE_EMBED_MODEL}...`);

  if (!VOYAGE_API_KEY) {
    throw new Error("VOYAGE_API_KEY not set — refusing to fake embeddings");
  }

  const response = await fetch(VOYAGE_EMBED_ENDPOINT, {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${VOYAGE_API_KEY}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      input: documents,
      model: VOYAGE_EMBED_MODEL,
    }),
  });
  if (!response.ok) {
    const body = await response.text();
    throw new Error(`Voyage embed ${response.status}: ${body.slice(0, 200)}`);
  }
  const data = (await response.json()) as VoyageEmbeddingResponse;
  const tokens = data.usage.total_tokens;
  const cost = (tokens / 1_000_000) * VOYAGE_EMBED_USD_PER_1M_TOKENS;

  const tracker = getTracker();
  await tracker.track(
    {
      taskType: "research_agent_embed",
      customerId: "dexcost-e2e",
      projectId: "agent-research",
      metadata: { agent: "research-agent-v2", step: "embed", model: VOYAGE_EMBED_MODEL },
    },
    async (task) => {
      task.recordCost(
        "voyageai-embed",
        cost,
        {
          model: VOYAGE_EMBED_MODEL,
          documents: documents.length,
          tokens,
          dim: data.data[0]?.embedding.length ?? 0,
        },
        "external_cost",
        "computed",
      );
      return task;
    },
  );

  console.log(
    `[agent] Embedded ${documents.length} docs, ${tokens} tokens, ` +
      `dim=${data.data[0]?.embedding.length}, cost=$${cost.toFixed(6)}`,
  );
  return tokens;
}

// ---------------------------------------------------------------------------
// Voyage AI: real rerank (HTTP)
// ---------------------------------------------------------------------------

interface VoyageRerankResponse {
  object: string;
  data: Array<{ index: number; relevance_score: number; document?: string }>;
  model: string;
  usage: { total_tokens: number };
}

async function semanticRerank(query: string, documents: string[]): Promise<string[]> {
  console.log(`[agent] Reranking ${documents.length} docs against query`);

  if (!VOYAGE_API_KEY) {
    throw new Error("VOYAGE_API_KEY not set — refusing to fake rerank");
  }

  const response = await fetch(VOYAGE_RERANK_ENDPOINT, {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${VOYAGE_API_KEY}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      query,
      documents,
      model: VOYAGE_RERANK_MODEL,
      top_k: Math.min(3, documents.length),
      return_documents: true,
    }),
  });
  if (!response.ok) {
    const body = await response.text();
    throw new Error(`Voyage rerank ${response.status}: ${body.slice(0, 200)}`);
  }
  const data = (await response.json()) as VoyageRerankResponse;
  // Rerank pricing is per-search not per-token, so 1 call = 1 search.
  const cost = (1 / 1_000) * VOYAGE_RERANK_USD_PER_1K_SEARCHES;

  const tracker = getTracker();
  await tracker.track(
    {
      taskType: "research_agent_rerank",
      customerId: "dexcost-e2e",
      projectId: "agent-research",
      metadata: { agent: "research-agent-v2", step: "rerank", model: VOYAGE_RERANK_MODEL },
    },
    async (task) => {
      task.recordCost(
        "voyageai-rerank",
        cost,
        {
          model: VOYAGE_RERANK_MODEL,
          documents: documents.length,
          tokens_used: data.usage.total_tokens,
          top_score: data.data[0]?.relevance_score ?? 0,
        },
        "external_cost",
        "computed",
      );
      return task;
    },
  );

  const ordered = data.data
    .sort((a, b) => b.relevance_score - a.relevance_score)
    .map((r) => r.document ?? documents[r.index]);
  console.log(`[agent] Rerank top score=${data.data[0]?.relevance_score.toFixed(3)}, cost=$${cost.toFixed(6)}`);
  return ordered;
}

// ---------------------------------------------------------------------------
// MiniMax M2.7: real LLM call (HTTP, Anthropic-compatible)
// ---------------------------------------------------------------------------

interface AnthropicMessageResponse {
  id: string;
  type: string;
  role: string;
  content: Array<{ type: string; text?: string }>;
  model: string;
  stop_reason: string;
  usage: { input_tokens: number; output_tokens: number };
}

async function llmReason(query: string, context: string[]): Promise<string> {
  console.log(`[agent] LLM reasoning with ${ANTHROPIC_MODEL}...`);

  if (!MINIMAX_API_KEY) {
    throw new Error("MINIMAX_API_KEY not set — refusing to fake LLM call");
  }

  const tracker = getTracker();
  let answer = "";

  await tracker.track(
    {
      taskType: "research_agent_llm",
      customerId: "dexcost-e2e",
      projectId: "agent-research",
      metadata: { agent: "research-agent-v2", step: "llm_reason", model: ANTHROPIC_MODEL },
    },
    async (task) => {
      const response = await fetch(MINIMAX_ENDPOINT, {
        method: "POST",
        headers: {
          "x-api-key": MINIMAX_API_KEY,
          "Content-Type": "application/json",
          "anthropic-version": "2023-06-01",
        },
        body: JSON.stringify({
          model: ANTHROPIC_MODEL,
          max_tokens: 256,
          messages: [
            {
              role: "user",
              content:
                `You are a concise research assistant.\n\nContext:\n` +
                context.map((c, i) => `(${i + 1}) ${c}`).join("\n") +
                `\n\nQuestion: ${query}\n\nAnswer in one short paragraph.`,
            },
          ],
        }),
      });

      if (!response.ok) {
        const body = await response.text();
        throw new Error(`MiniMax ${response.status}: ${body.slice(0, 200)}`);
      }
      const data = (await response.json()) as AnthropicMessageResponse;
      const inputTokens = data.usage?.input_tokens ?? 0;
      const outputTokens = data.usage?.output_tokens ?? 0;
      const cost =
        (inputTokens / 1_000_000) * MINIMAX_INPUT_USD_PER_1M +
        (outputTokens / 1_000_000) * MINIMAX_OUTPUT_USD_PER_1M;

      task.recordLlmCall(
        "minimax",
        ANTHROPIC_MODEL,
        inputTokens,
        outputTokens,
        cost,
        undefined,
        0,
        "computed",
      );

      const text = data.content?.find((b) => b.type === "text")?.text ?? "(no text block)";
      console.log(
        `[agent] LLM ${inputTokens}in/${outputTokens}out cost=$${cost.toFixed(6)}: ` +
          text.slice(0, 80) +
          (text.length > 80 ? "..." : ""),
      );
      answer = text;
      return task;
    },
  );

  return answer;
}

// ---------------------------------------------------------------------------
// Retry waste — first attempt 429, second succeeds
// ---------------------------------------------------------------------------

async function recordRetryWaste(): Promise<void> {
  console.log("[agent] Recording retry waste (rate-limit hit)...");
  const tracker = getTracker();

  await tracker.track(
    {
      taskType: "research_agent_retry",
      customerId: "dexcost-e2e",
      projectId: "agent-research",
      metadata: { agent: "research-agent-v2", step: "retry" },
    },
    async (task) => {
      // Pretend the first call burned 200 input / 0 output tokens before being
      // rate-limited. recordCost with event_type retry_marker captures the waste.
      const wastedInputTokens = 200;
      const wastedCost = (wastedInputTokens / 1_000_000) * MINIMAX_INPUT_USD_PER_1M;
      task.recordCost(
        "minimax-retry-429",
        wastedCost,
        {
          reason: "rate_limit_hit",
          wasted_input_tokens: wastedInputTokens,
          model: ANTHROPIC_MODEL,
        },
        "retry_marker",
        "computed",
      );
      task.markRetry("rate_limit_hit", wastedCost);
      return task;
    },
  );

  console.log("[agent] Retry waste recorded.");
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

async function run(): Promise<void> {
  console.log(`\n=== Dexcost E2E Research Agent (v2) ===`);
  console.log(`Endpoint:        ${DEXCOST_API_URL}`);
  console.log(`API key prefix:  ${DEXCOST_API_KEY ? DEXCOST_API_KEY.slice(0, 12) + "..." : "(not set)"}`);
  console.log(`LLM model:       ${ANTHROPIC_MODEL}`);
  console.log(`Embed model:     ${VOYAGE_EMBED_MODEL}`);
  console.log(`Rerank model:    ${VOYAGE_RERANK_MODEL}`);
  console.log(`========================================\n`);

  if (!DEXCOST_API_KEY) {
    throw new Error("DEXCOST_API_KEY not set");
  }

  // Set DEXCOST_ENDPOINT so the SDK pusher targets the local control layer
  process.env.DEXCOST_ENDPOINT = DEXCOST_API_URL;

  init({
    apiKey: DEXCOST_API_KEY,
    autoInstrument: [],
    flushIntervalMs: 5000,
    batchSize: 50,
  });

  const docs = [
    "Dexcost is an agent unit economics platform that attributes LLM costs, non-LLM service fees, and retry waste to customers, projects, and workflows.",
    "Agent cost observability is critical for AI-native businesses to understand true unit economics and unit-level profitability.",
    "Voyage AI provides state-of-the-art embedding models for semantic search and RAG pipelines; voyage-3-large outperforms OpenAI text-embedding-3 on MTEB.",
    "Cost tracking should attribute every dollar to a specific customer, project, and workflow run for accurate unit economics.",
    "MiniMax M2.7 provides Anthropic-compatible API access; it accepts /v1/messages payloads with anthropic-version header and returns input_tokens and output_tokens.",
    "Retry waste is when an agent retries a failed API call; the wasted compute is real cost that should be tracked separately from useful cost.",
  ];
  const query = "How does Dexcost track agent costs across LLM and non-LLM services?";

  // Step 1 — embed (real Voyage)
  await embedDocuments(docs);

  // Step 2 — rerank (real Voyage)
  const ranked = await semanticRerank(query, docs);

  // Step 3 — reason (real MiniMax)
  const top3 = ranked.slice(0, 3);
  const answer = await llmReason(query, top3);

  // Step 4 — retry waste
  await recordRetryWaste();

  console.log(`\n=== Agent answer ===`);
  console.log(answer);
  console.log(`====================\n`);

  // Flush events synchronously so the test report sees them
  await getTracker().flush();
  await closeAsync();
  console.log("[agent] Run complete.");
}

run().catch((err) => {
  console.error("[agent] Error:", err);
  process.exit(1);
});
