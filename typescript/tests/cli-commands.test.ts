/**
 * Tests for CLI status and rates commands (US-049).
 *
 * Invokes the CLI directly via node + vite-node for cross-platform compat.
 */

import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { spawnSync } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync, existsSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { randomUUID } from "node:crypto";
import { EventBuffer } from "../src/transport/buffer.js";
import { createCostEvent, createTask } from "../src/core/models.js";

// When vitest is run from sdks/typescript/, process.cwd() is already that dir.
// Avoid doubling the path by using cwd() directly if it ends in "sdks/typescript".
const _cwd = process.cwd();
const SDK_DIR = _cwd.endsWith("sdks/typescript") || _cwd.endsWith("sdks\\typescript")
  ? _cwd
  : join(_cwd, "sdks/typescript");

/**
 * Find the node.exe binary that matches the current process's Node.js version.
 *
 * On Windows, cmd.exe may not be in PATH when vitest workers run (shell:true
 * breaks). We use where.exe (always at a fixed system32 path) to enumerate
 * node installations, then pick the one with the same major.minor as
 * process.versions.node so native addons (better-sqlite3) load correctly.
 */
function findNodeBin(): string {
  const currentVersion = process.versions.node; // e.g. "24.13.1"
  const [curMajor, curMinor] = currentVersion.split(".").map(Number);

  const whereExe = "C:\\Windows\\System32\\where.exe";
  if (existsSync(whereExe)) {
    const r = spawnSync(whereExe, ["node"], { encoding: "utf-8" });
    if (!r.error && r.stdout) {
      const lines = r.stdout.split(/\r?\n/).map((l) => l.trim()).filter(Boolean);
      // First pass: find exact version match
      for (const line of lines) {
        if (!line.toLowerCase().endsWith("node.exe")) continue;
        const vr = spawnSync(line, ["--version"], { encoding: "utf-8" });
        if (vr.error) continue;
        const v = vr.stdout.trim().replace(/^v/, ""); // "24.13.1"
        const [maj, min] = v.split(".").map(Number);
        if (maj === curMajor && min === curMinor) return line;
      }
      // Second pass: same major
      for (const line of lines) {
        if (!line.toLowerCase().endsWith("node.exe")) continue;
        const vr = spawnSync(line, ["--version"], { encoding: "utf-8" });
        if (vr.error) continue;
        const [maj] = vr.stdout.trim().replace(/^v/, "").split(".").map(Number);
        if (maj === curMajor) return line;
      }
    }
  }
  // Fallback: process.execPath
  return process.execPath;
}

const NODE_BIN = findNodeBin();
const VITE_NODE_MJS = join(SDK_DIR, "node_modules", "vite-node", "vite-node.mjs");

function runCli(args: string[], cwd = SDK_DIR): string {
  const result = spawnSync(
    NODE_BIN,
    [VITE_NODE_MJS, "src/cli/index.ts", ...args],
    {
      cwd,
      encoding: "utf-8",
      env: { ...process.env },
    }
  );
  if (result.error) throw result.error;
  // Combine stdout + stderr; some output may go to stderr
  return (result.stdout ?? "") + (result.stderr ?? "");
}

let tmpDir: string;

beforeEach(() => {
  tmpDir = mkdtempSync(join(tmpdir(), "dexcost-cli-test-"));
});

afterEach(() => {
  rmSync(tmpDir, { recursive: true, force: true });
});

describe("CLI: status command", () => {
  it("shows DB info for an existing database with events and tasks", () => {
    const dbPath = join(tmpDir, "test.db");

    // Create a buffer, add one event and one task, then close
    const buffer = new EventBuffer(dbPath);
    const taskId = randomUUID();

    const task = createTask({
      taskId,
      taskType: "test_task",
      status: "success",
    });
    buffer.upsertTask(task);

    const event = createCostEvent({
      eventId: randomUUID(),
      taskId,
      eventType: "llm_call",
      costUsd: 0.05,
    });
    buffer.addEvent(event);
    buffer.close();

    const output = runCli(["status", "--db", dbPath]);

    expect(output).toContain("DB location:");
    expect(output).toContain(dbPath);
    expect(output).toContain("Events:");
    expect(output).toContain("1");
    expect(output).toContain("Tasks:");
    expect(output).toContain("Pending:");
    expect(output).toContain("Synced:");
    expect(output).toContain("Pricing version:");
  });

  it("shows 'not found' message for a missing database", () => {
    const missingPath = join(tmpDir, "nonexistent.db");

    const output = runCli(["status", "--db", missingPath]);

    expect(output).toContain("DB location:");
    expect(output).toContain(missingPath);
    expect(output).toContain("Status: Database not found");
  });
});

describe("CLI: rates command", () => {
  it("imports rates and lists them in a formatted table", () => {
    const ratesFile = join(tmpDir, "rates.yaml");
    writeFileSync(
      ratesFile,
      "rates:\n  stripe_fee:\n    per: transaction\n    cost_usd: 0.005\n  twilio_sms:\n    per: message\n    cost_usd: 0.0075\n",
      "utf-8"
    );

    const output = runCli(["rates", "--import", ratesFile, "--list"]);

    expect(output).toContain("Loaded");
    expect(output).toContain("2");
    // Table headers
    expect(output).toContain("Service");
    expect(output).toContain("Per");
    expect(output).toContain("Cost USD");
    // Rate entries (sorted by service)
    expect(output).toContain("stripe_fee");
    expect(output).toContain("twilio_sms");
  });

  it("imports rates and exports them to a new file (round-trip)", () => {
    const importFile = join(tmpDir, "rates-in.yaml");
    const exportFile = join(tmpDir, "rates-out.yaml");

    writeFileSync(
      importFile,
      "rates:\n  sendgrid_email:\n    per: email\n    cost_usd: 0.001\n",
      "utf-8"
    );

    const output = runCli(["rates", "--import", importFile, "--export", exportFile]);

    expect(output).toContain("Loaded");
    expect(output).toContain("1");
    expect(output).toContain("Exported");
    expect(output).toContain("1");

    // Verify the exported file can be re-imported
    const reImportOutput = runCli(["rates", "--import", exportFile, "--list"]);
    expect(reImportOutput).toContain("sendgrid_email");
  });
});
