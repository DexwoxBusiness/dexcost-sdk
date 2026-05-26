/**
 * JSON Schema validation for dexcost Standard Event Schema v1.
 *
 * Validates task and event payloads against bundled JSON Schema v1 files
 * using Ajv. Mirrors the Python SDK's schema.py validate() function.
 */

// Sprint 3 Theme E / §4.2.3 — runtime JSON load for Node 18 support.
// `import x from "./x.json" with { type: "json" }` is Node 22+ syntax;
// reading the file at runtime works on every supported Node version.
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const _thisDir = dirname(fileURLToPath(import.meta.url));
const eventSchema = JSON.parse(
  readFileSync(join(_thisDir, "dexcost-event.v1.json"), "utf-8"),
);
const taskSchema = JSON.parse(
  readFileSync(join(_thisDir, "dexcost-task.v1.json"), "utf-8"),
);

// eslint-disable-next-line @typescript-eslint/no-explicit-any
let ajv: any;
// eslint-disable-next-line @typescript-eslint/no-explicit-any
let validateEvent: any;
// eslint-disable-next-line @typescript-eslint/no-explicit-any
let validateTask: any;

try {
  // eslint-disable-next-line @typescript-eslint/no-require-imports
  const AjvModule = require("ajv");
  const AjvClass = AjvModule.default ?? AjvModule;

  // Build Ajv instance with allErrors so all problems are reported at once.
  ajv = new AjvClass({ allErrors: true });

  // Attempt to load ajv-formats for uuid and date-time format validation.
  try {
    // eslint-disable-next-line @typescript-eslint/no-require-imports
    const formats = require("ajv-formats");
    const addFormats = formats.default ?? formats;
    addFormats(ajv);
  } catch {
    /* ajv-formats not available — format keywords are silently ignored */
  }

  validateEvent = ajv.compile(
    eventSchema as Parameters<typeof ajv.compile>[0]
  );
  validateTask = ajv.compile(
    taskSchema as Parameters<typeof ajv.compile>[0]
  );
} catch {
  // ajv not installed — validators will be null, validate() returns empty errors
  ajv = null;
  validateEvent = null;
  validateTask = null;
}

/**
 * Validate a task or event payload against the bundled JSON Schema v1.
 *
 * @param payload - A Record produced by `taskToDict()` or `eventToDict()`.
 * @returns An empty array when the payload is valid; otherwise an array of
 *          human-readable error strings in `"path: message"` format.
 */
export function validate(payload: Record<string, unknown>): string[] {
  // If ajv failed to load, we can't validate — return empty (no errors)
  if (ajv === null || validateEvent === null || validateTask === null) {
    return [];
  }

  // Step 1: check schema_version
  const sv = payload["schema_version"];
  if (sv !== "1") {
    return [`Unsupported schema_version: ${String(sv)}`];
  }

  // Step 2: route to the correct schema validator
  if ("event_id" in payload) {
    return runValidator(validateEvent, payload);
  }

  if ("task_id" in payload) {
    return runValidator(validateTask, payload);
  }

  // Step 3: cannot determine type
  return ["Cannot determine payload type: missing task_id or event_id"];
}

/** Run an Ajv compiled validator and format its errors. */
function runValidator(
  validator: ReturnType<typeof ajv.compile>,
  payload: unknown
): string[] {
  const valid = validator(payload);
  if (valid) {
    return [];
  }
  const errors = validator.errors ?? [];
  return errors.map((err: { instancePath?: string; message?: string }) => {
    const path = err.instancePath || "(root)";
    const message = err.message ?? "unknown error";
    return `${path}: ${message}`;
  });
}
