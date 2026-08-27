import { readFileSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import ts from "typescript";

const scriptDir = dirname(fileURLToPath(import.meta.url));
const tsRoot = resolve(scriptDir, "..");
const sdkRoot = resolve(tsRoot, "..");
const freezeRoot = join(sdkRoot, "contracts", "python-vnext", "v1");
const pythonApiPath = join(freezeRoot, "public-api.json");
const tsApiPath = join(freezeRoot, "typescript-public-api.json");
const parityPath = join(freezeRoot, "typescript-api-map.json");
const packageJson = JSON.parse(readFileSync(join(tsRoot, "package.json"), "utf-8"));
const pythonApi = JSON.parse(readFileSync(pythonApiPath, "utf-8"));

const indexPath = join(tsRoot, "src", "index.ts");
const configFile = ts.readConfigFile(join(tsRoot, "tsconfig.json"), ts.sys.readFile);
if (configFile.error) throw new Error(ts.flattenDiagnosticMessageText(configFile.error.messageText, "\n"));
const parsedConfig = ts.parseJsonConfigFileContent(configFile.config, ts.sys, tsRoot);
const program = ts.createProgram([indexPath], { ...parsedConfig.options, noEmit: true });
const diagnostics = ts.getPreEmitDiagnostics(program);
if (diagnostics.length > 0) {
  const host = {
    getCanonicalFileName: (name) => name,
    getCurrentDirectory: () => tsRoot,
    getNewLine: () => "\n",
  };
  throw new Error(ts.formatDiagnosticsWithColorAndContext(diagnostics, host));
}
const checker = program.getTypeChecker();
const source = program.getSourceFile(indexPath);
if (!source) throw new Error(`Cannot load ${indexPath}`);
const moduleSymbol = checker.getSymbolAtLocation(source);
if (!moduleSymbol) throw new Error(`Cannot resolve module symbol for ${indexPath}`);
const exportedSymbols = checker.getExportsOfModule(moduleSymbol)
  .map((symbol) => symbol.getName())
  .sort();
const exported = new Set(exportedSymbols);

const overrides = new Map(Object.entries({
  AttachedTask: ["TrackedTask"],
  DexcostConfig: ["TrackerOptions", "ResolvedConfig"],
  Event: ["CostEvent"],
  SyncWorker: ["EventPusher"],
  async_task_context: ["runWithTask"],
  task_context: ["runWithTask"],
  to_business_identity_revision_v1: ["toBusinessIdentityRevision"],
  instrument_litellm: ["instrumentLiteLLM"],
  instrument_gemini: ["instrumentGoogleGenAI"],
  instrument_openai: ["instrumentOpenAI"],
  instrument_openrouter: ["instrumentOpenRouter"],
  uninstrument_litellm: ["uninstrumentLiteLLM"],
  uninstrument_gemini: ["uninstrumentGoogleGenAI"],
  uninstrument_openai: ["uninstrumentOpenAI"],
  uninstrument_openrouter: ["uninstrumentOpenRouter"],
}));

const equivalenceNotes = new Map(Object.entries({
  ALL_SUPPORTED_INSTRUMENTS:
    "Equivalent supported-instrument registry; TypeScript additionally exposes its Vercel AI and legacy @google/generative-ai adapters, while Python's gemini entry maps to the current google.genai adapter.",
  instrument_gemini:
    "Python google.genai maps to TypeScript @google/genai; instrumentGemini is the separate legacy @google/generative-ai adapter.",
  uninstrument_gemini:
    "Python google.genai maps to TypeScript @google/genai; uninstrumentGemini is the separate legacy @google/generative-ai adapter.",
}));

const languageSpecific = new Map(Object.entries({
  __version__: "Node package version is authoritative in package.json rather than a root runtime export.",
  track_crewai: "CrewAI is a Python-only framework; TypeScript exposes equivalent generic task/tool/capability primitives.",
  track_griptape: "Griptape is a Python-only framework; TypeScript exposes equivalent generic task/tool/capability primitives.",
}));

function lowerCamel(name) {
  return name.replace(/_([a-z0-9])/g, (_match, char) => char.toUpperCase());
}

const unresolved = [];
const mappings = pythonApi.exports.map((entry) => {
  const name = entry.name;
  if (languageSpecific.has(name)) {
    return {
      python_name: name,
      python_kind: entry.kind,
      classification: "language_specific",
      typescript_exports: [],
      notes: languageSpecific.get(name),
    };
  }
  const candidates = overrides.get(name)
    ?? (exported.has(name) ? [name] : [lowerCamel(name)]);
  const missing = candidates.filter((candidate) => !exported.has(candidate));
  if (missing.length > 0) unresolved.push(`${name} -> ${missing.join(", ")}`);
  return {
    python_name: name,
    python_kind: entry.kind,
    classification: "equivalent",
    typescript_exports: candidates,
    notes: equivalenceNotes.get(name)
      ?? (overrides.has(name) ? "Intentional TypeScript naming/API-shape equivalent." : null),
  };
});

if (unresolved.length > 0) {
  throw new Error(`Unresolved Python public exports:\n${unresolved.join("\n")}`);
}

const tsApi = {
  freeze_version: 1,
  package: packageJson.name,
  root_entrypoint: "src/index.ts",
  exports: exportedSymbols,
};
const parity = {
  freeze_version: 1,
  authority: "contracts/python-vnext/v1/public-api.json",
  python_export_count: pythonApi.exports.length,
  typescript_export_count: exportedSymbols.length,
  equivalent_count: mappings.filter((item) => item.classification === "equivalent").length,
  language_specific_count: mappings.filter((item) => item.classification === "language_specific").length,
  unresolved_count: 0,
  mappings,
};

const serialized = (value) => `${JSON.stringify(value, null, 2)}\n`;
const outputs = [[tsApiPath, serialized(tsApi)], [parityPath, serialized(parity)]];
if (process.argv.includes("--write")) {
  for (const [path, content] of outputs) writeFileSync(path, content, "utf-8");
  process.stdout.write(`Wrote ${outputs.length} TypeScript contract freeze artifacts.\n`);
} else {
  const drift = outputs
    .filter(([path, content]) => {
      try { return readFileSync(path, "utf-8") !== content; } catch { return true; }
    })
    .map(([path]) => path);
  if (drift.length > 0) {
    throw new Error(`TypeScript contract freeze drift:\n${drift.join("\n")}\nRun: node scripts/freeze-contract.mjs --write`);
  }
  process.stdout.write("TypeScript contract freeze is current.\n");
}
