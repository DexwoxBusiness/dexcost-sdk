/**
 * Code scanner for cost point detection (US-031).
 *
 * Regex-based static analysis — no API key needed, runs offline.
 *
 * Detection layers:
 * 1. LLM SDK calls (auto-instrumentable by dexcost or manual)
 * 2. Agent framework invocations (LangChain, Mastra, CrewAI, etc.)
 * 3. Direct HTTP calls to known AI API endpoints
 * 4. SDK imports for paid services (AWS, Stripe, Firebase, etc.)
 * 5. Service-specific method calls (MongoDB, Elasticsearch, etc.)
 */

export interface CostPoint {
  file: string;
  line: number;
  provider: string;
  type:
    | "llm_call"
    | "http_call"
    | "sdk_import"
    | "framework_call"
    | "service_call"
    | "embedding"
    | "speech"
    | "image";
  pattern: string;
  autoInstrumentable: boolean;
}

interface ScanPattern {
  regex: RegExp;
  provider: string;
  type: CostPoint["type"];
  autoInstrumentable: boolean;
}

const PATTERNS: ScanPattern[] = [
  // ── LLM SDK calls (auto-instrumented by dexcost) ────────────────────
  {
    regex: /\.chat\s*\.\s*completions\s*\.\s*create\b/g,
    provider: "openai",
    type: "llm_call",
    autoInstrumentable: true,
  },
  {
    regex: /\.messages\s*\.\s*create\b/g,
    provider: "anthropic",
    type: "llm_call",
    autoInstrumentable: true,
  },
  {
    regex: /\b(generateText|streamText)\s*\(/g,
    provider: "vercel-ai",
    type: "llm_call",
    autoInstrumentable: true,
  },

  // ── Additional LLM providers (need recordCost) ──────────────────────
  // Groq — uses OpenAI-compatible SDK: groq-sdk
  {
    regex: /from\s+["']groq-sdk["']|require\s*\(\s*["']groq-sdk["']\s*\)/g,
    provider: "groq",
    type: "sdk_import",
    autoInstrumentable: false,
  },
  // Mistral — @mistralai/mistralai, uses client.chat.complete()
  {
    regex: /\.chat\s*\.\s*complete\b/g,
    provider: "mistral",
    type: "llm_call",
    autoInstrumentable: false,
  },
  {
    regex: /from\s+["']@mistralai\/mistralai["']/g,
    provider: "mistral",
    type: "sdk_import",
    autoInstrumentable: false,
  },
  // Together AI — together-ai
  {
    regex: /from\s+["']together-ai["']|require\s*\(\s*["']together-ai["']\s*\)/g,
    provider: "together",
    type: "sdk_import",
    autoInstrumentable: false,
  },
  // Replicate — replicate.run()
  {
    regex: /replicate\s*\.\s*run\s*\(/g,
    provider: "replicate",
    type: "llm_call",
    autoInstrumentable: false,
  },
  {
    regex: /from\s+["']replicate["']|require\s*\(\s*["']replicate["']\s*\)/g,
    provider: "replicate",
    type: "sdk_import",
    autoInstrumentable: false,
  },
  // Fireworks AI
  {
    regex: /from\s+["']@fireworks-ai\/client["']/g,
    provider: "fireworks",
    type: "sdk_import",
    autoInstrumentable: false,
  },
  // Cohere — cohere.chat() / cohere.generate()
  {
    regex: /\bcohere\s*\.\s*(chat|generate|embed)\s*\(/g,
    provider: "cohere",
    type: "llm_call",
    autoInstrumentable: true,
  },
  {
    regex: /from\s+["']cohere-ai["']|require\s*\(\s*["']cohere-ai["']\s*\)/g,
    provider: "cohere",
    type: "sdk_import",
    autoInstrumentable: true,
  },
  // Google Gemini — @google/genai, generateContent
  {
    regex: /\.generateContent\s*\(/g,
    provider: "google-ai",
    type: "llm_call",
    autoInstrumentable: true,
  },
  // Ollama
  {
    regex: /from\s+["']ollama["']|require\s*\(\s*["']ollama["']\s*\)/g,
    provider: "ollama",
    type: "sdk_import",
    autoInstrumentable: false,
  },

  // ── Embeddings ──────────────────────────────────────────────────────
  {
    regex: /\.embeddings\s*\.\s*create\s*\(/g,
    provider: "openai",
    type: "embedding",
    autoInstrumentable: false,
  },

  // ── Speech / Audio ──────────────────────────────────────────────────
  {
    regex: /\.audio\s*\.\s*transcriptions\s*\.\s*create\s*\(/g,
    provider: "openai-whisper",
    type: "speech",
    autoInstrumentable: false,
  },
  {
    regex: /\.audio\s*\.\s*speech\s*\.\s*create\s*\(/g,
    provider: "openai-tts",
    type: "speech",
    autoInstrumentable: false,
  },
  {
    regex: /from\s+["']@deepgram\/sdk["']/g,
    provider: "deepgram",
    type: "sdk_import",
    autoInstrumentable: false,
  },
  {
    regex: /from\s+["']assemblyai["']/g,
    provider: "assemblyai",
    type: "sdk_import",
    autoInstrumentable: false,
  },

  // ── Image generation ────────────────────────────────────────────────
  {
    regex: /\.images\s*\.\s*generate\s*\(/g,
    provider: "openai-dalle",
    type: "image",
    autoInstrumentable: false,
  },

  // ── Agent framework calls ───────────────────────────────────────────
  // LangChain.js — .invoke(), .stream(), .batch()
  {
    regex: /from\s+["'](@langchain\/openai|@langchain\/anthropic|@langchain\/core|langchain)["']/g,
    provider: "langchain",
    type: "sdk_import",
    autoInstrumentable: false,
  },
  {
    regex: /\.(invoke|ainvoke|stream|batch)\s*\(/g,
    provider: "langchain",
    type: "framework_call",
    autoInstrumentable: false,
  },
  // LangGraph
  {
    regex: /from\s+["']@langchain\/langgraph["']/g,
    provider: "langgraph",
    type: "sdk_import",
    autoInstrumentable: false,
  },
  // Mastra
  {
    regex: /from\s+["']@mastra\/core["']/g,
    provider: "mastra",
    type: "sdk_import",
    autoInstrumentable: false,
  },
  // CrewAI (JS)
  {
    regex: /from\s+["']crewai["']/g,
    provider: "crewai",
    type: "sdk_import",
    autoInstrumentable: false,
  },

  // ── Direct HTTP calls to known AI API endpoints ─────────────────────
  {
    regex: /fetch\s*\(\s*["'`]https?:\/\/api\.openai\.com/g,
    provider: "openai",
    type: "http_call",
    autoInstrumentable: false,
  },
  {
    regex: /fetch\s*\(\s*["'`]https?:\/\/api\.anthropic\.com/g,
    provider: "anthropic",
    type: "http_call",
    autoInstrumentable: false,
  },
  {
    regex: /fetch\s*\(\s*["'`]https?:\/\/api\.cohere\.(ai|com)/g,
    provider: "cohere",
    type: "http_call",
    autoInstrumentable: false,
  },
  {
    regex: /fetch\s*\(\s*["'`]https?:\/\/api\.groq\.com/g,
    provider: "groq",
    type: "http_call",
    autoInstrumentable: false,
  },
  {
    regex: /fetch\s*\(\s*["'`]https?:\/\/api\.mistral\.ai/g,
    provider: "mistral",
    type: "http_call",
    autoInstrumentable: false,
  },
  {
    regex: /fetch\s*\(\s*["'`]https?:\/\/api\.together\.xyz/g,
    provider: "together",
    type: "http_call",
    autoInstrumentable: false,
  },
  {
    regex: /fetch\s*\(\s*["'`]https?:\/\/api\.replicate\.com/g,
    provider: "replicate",
    type: "http_call",
    autoInstrumentable: false,
  },
  {
    regex: /fetch\s*\(\s*["'`]https?:\/\/api\.deepseek\.com/g,
    provider: "deepseek",
    type: "http_call",
    autoInstrumentable: false,
  },

  // ── Cloud / Infrastructure SDK imports ──────────────────────────────
  {
    regex: /@aws-sdk\/client-bedrock-runtime/g,
    provider: "aws-bedrock",
    type: "sdk_import",
    autoInstrumentable: true,
  },
  {
    regex: /@aws-sdk\/client-/g,
    provider: "aws",
    type: "sdk_import",
    autoInstrumentable: false,
  },
  {
    regex: /@google-ai\/generativelanguage|@google\/generative-ai|@google\/genai/g,
    provider: "google-ai",
    type: "sdk_import",
    autoInstrumentable: true,
  },
  {
    regex: /from\s+["']@google-cloud\//g,
    provider: "gcp",
    type: "sdk_import",
    autoInstrumentable: false,
  },

  // ── Payments ────────────────────────────────────────────────────────
  {
    regex: /from\s+["']stripe["']|require\s*\(\s*["']stripe["']\s*\)/g,
    provider: "stripe",
    type: "sdk_import",
    autoInstrumentable: false,
  },
  {
    regex: /stripe\s*\.\s*(paymentIntents|charges|customers|subscriptions|invoices)\s*\.\s*(create|retrieve|update|list)\s*\(/g,
    provider: "stripe",
    type: "service_call",
    autoInstrumentable: false,
  },

  // ── Databases ───────────────────────────────────────────────────────
  // MongoDB
  {
    regex: /from\s+["']mongodb["']|require\s*\(\s*["']mongodb["']\s*\)/g,
    provider: "mongodb",
    type: "sdk_import",
    autoInstrumentable: false,
  },
  {
    regex: /\.\s*(find|findOne|insertOne|insertMany|updateOne|updateMany|deleteOne|deleteMany|aggregate)\s*\(/g,
    provider: "mongodb",
    type: "service_call",
    autoInstrumentable: false,
  },
  // Supabase
  {
    regex: /from\s+["']@supabase\/supabase-js["']/g,
    provider: "supabase",
    type: "sdk_import",
    autoInstrumentable: false,
  },
  // Firebase
  {
    regex: /from\s+["']firebase-admin["']|from\s+["']firebase\/firestore["']/g,
    provider: "firebase",
    type: "sdk_import",
    autoInstrumentable: false,
  },

  // ── Search ──────────────────────────────────────────────────────────
  {
    regex: /from\s+["']@elastic\/elasticsearch["']/g,
    provider: "elasticsearch",
    type: "sdk_import",
    autoInstrumentable: false,
  },

  // ── Cache ───────────────────────────────────────────────────────────
  {
    regex: /from\s+["'](ioredis|redis)["']|require\s*\(\s*["'](ioredis|redis)["']\s*\)/g,
    provider: "redis",
    type: "sdk_import",
    autoInstrumentable: false,
  },

  // ── Vector databases ────────────────────────────────────────────────
  {
    regex: /from\s+["']@pinecone-database\/pinecone["']/g,
    provider: "pinecone",
    type: "sdk_import",
    autoInstrumentable: false,
  },
  {
    regex: /\.\s*(query|upsert)\s*\(/g,
    provider: "vector-db",
    type: "service_call",
    autoInstrumentable: false,
  },
  {
    regex: /from\s+["']weaviate-ts-client["']|from\s+["']weaviate-client["']/g,
    provider: "weaviate",
    type: "sdk_import",
    autoInstrumentable: false,
  },
  {
    regex: /from\s+["']chromadb["']/g,
    provider: "chromadb",
    type: "sdk_import",
    autoInstrumentable: false,
  },
  {
    regex: /from\s+["']@qdrant\/js-client-rest["']/g,
    provider: "qdrant",
    type: "sdk_import",
    autoInstrumentable: false,
  },

  // ── Messaging / Communications ──────────────────────────────────────
  {
    regex: /from\s+["']twilio["']|require\s*\(\s*["']twilio["']\s*\)/g,
    provider: "twilio",
    type: "sdk_import",
    autoInstrumentable: false,
  },
  {
    regex: /from\s+["']@sendgrid\/mail["']/g,
    provider: "sendgrid",
    type: "sdk_import",
    autoInstrumentable: false,
  },
  {
    regex: /from\s+["']resend["']/g,
    provider: "resend",
    type: "sdk_import",
    autoInstrumentable: false,
  },
  {
    regex: /from\s+["']@slack\/web-api["']/g,
    provider: "slack",
    type: "sdk_import",
    autoInstrumentable: false,
  },

  // ── Geo / Maps ──────────────────────────────────────────────────────
  {
    regex: /from\s+["']@googlemaps\/google-maps-services-js["']/g,
    provider: "google-maps",
    type: "sdk_import",
    autoInstrumentable: false,
  },

  // ── Web scraping / data ─────────────────────────────────────────────
  {
    regex: /from\s+["']@mendable\/firecrawl-js["']/g,
    provider: "firecrawl",
    type: "sdk_import",
    autoInstrumentable: false,
  },
  {
    regex: /from\s+["']@tavily\/core["']/g,
    provider: "tavily",
    type: "sdk_import",
    autoInstrumentable: false,
  },
];

/**
 * Scan a source string for cost-generating patterns.
 *
 * @param source - File contents to scan.
 * @param fileName - Path to include in results.
 * @returns Array of detected cost points.
 */
export function scanSource(source: string, fileName: string): CostPoint[] {
  const results: CostPoint[] = [];
  const seenLines = new Set<string>(); // "provider:line" dedup key

  for (const pattern of PATTERNS) {
    // Reset regex state for each file
    pattern.regex.lastIndex = 0;
    let match: RegExpExecArray | null;

    while ((match = pattern.regex.exec(source)) !== null) {
      const beforeMatch = source.slice(0, match.index);
      const lineNumber = beforeMatch.split("\n").length;

      // Deduplicate: same provider on same line
      const key = `${pattern.provider}:${lineNumber}`;
      if (seenLines.has(key)) continue;
      seenLines.add(key);

      results.push({
        file: fileName,
        line: lineNumber,
        provider: pattern.provider,
        type: pattern.type,
        pattern: match[0].trim(),
        autoInstrumentable: pattern.autoInstrumentable,
      });
    }
  }

  return results;
}

/**
 * Generate integration stub code from detected cost points.
 *
 * Produces a self-contained template showing:
 * 1. SDK initialisation
 * 2. Customer/project context
 * 3. Task wrapper with manual `recordCost` calls
 * 4. Summary of auto-instrumented providers
 */
export function generateStubs(points: CostPoint[], _scanDir: string): string {
  const auto = points.filter((p) => p.autoInstrumentable);
  const manual = points.filter((p) => !p.autoInstrumentable);

  const lines: string[] = [
    "// ============================================================",
    "// dexcost integration stubs",
    "// Generated by: dexcost scan --generate-stubs",
    "// ============================================================",
    "",
    "// --- Step 1: Initialize dexcost ---",
    'import { init, track } from "@dexcost/sdk";',
    "",
    'init({ apiKey: "dx_live_..." }); // or set DEXCOST_API_KEY env var',
    "",
    "// --- Step 2: Set customer context (in your request handler) ---",
    'import { setContext } from "@dexcost/sdk";',
    "",
    "setContext({",
    '  customerId: "your_customer_id",',
    '  projectId: "your_project_id",',
    "});",
    "",
    "// --- Step 3: Track tasks ---",
    'await track({ taskType: "your_task_type" }, async (task) => {',
    "  // Your agent code here...",
    "",
  ];

  if (manual.length > 0) {
    lines.push(
      "  // --- Manual cost tracking (for services not auto-instrumented) ---",
    );
    for (const p of manual) {
      lines.push(`  // ${p.file}:${p.line} \u2014 ${p.pattern}`);
      lines.push(
        `  task.recordCost("${p.provider}", 0.00); // TODO: set actual cost`,
      );
      lines.push("");
    }
  }

  lines.push("});");
  lines.push("");

  if (auto.length > 0) {
    lines.push("// --- Auto-instrumented (no code changes needed) ---");
    const providers = new Map<string, number>();
    for (const p of auto) {
      providers.set(p.provider, (providers.get(p.provider) ?? 0) + 1);
    }
    for (const [provider, count] of providers) {
      lines.push(
        `// \u2713 ${provider} (${count} call${count > 1 ? "s" : ""} detected)`,
      );
    }
  }

  return lines.join("\n") + "\n";
}
