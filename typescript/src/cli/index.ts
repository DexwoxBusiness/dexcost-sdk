#!/usr/bin/env node
/**
 * dexcost CLI.
 *
 * Commands:
 *   dexcost scan [directory]         — scan TypeScript/JavaScript projects for cost points (US-031)
 *   dexcost status [--db path]       — show local buffer DB stats and SDK versions (US-049)
 *   dexcost rates --list             — list all rates in the registry
 *   dexcost rates --import path      — import rates from a YAML file
 *   dexcost rates --export path      — export rates to a YAML file
 */

import { existsSync, readFileSync, readdirSync, statSync } from "node:fs";
import { homedir } from "node:os";
import { join, extname } from "node:path";
import { scanSource, generateStubs } from "./scanner.js";
import { EventBuffer } from "../transport/buffer.js";
import { PricingEngine } from "../pricing/engine.js";
import { RateRegistry } from "../pricing/rates.js";

// ---------------------------------------------------------------------------
// scan helpers
// ---------------------------------------------------------------------------

const EXTENSIONS = new Set([".ts", ".tsx", ".js", ".jsx", ".mjs"]);
const IGNORE = new Set(["node_modules", "dist", ".git", "coverage"]);

function walkDir(dir: string): string[] {
  const files: string[] = [];

  for (const entry of readdirSync(dir)) {
    if (IGNORE.has(entry)) continue;

    const fullPath = join(dir, entry);
    const stat = statSync(fullPath);

    if (stat.isDirectory()) {
      files.push(...walkDir(fullPath));
    } else if (EXTENSIONS.has(extname(entry))) {
      files.push(fullPath);
    }
  }

  return files;
}

// ---------------------------------------------------------------------------
// scan command
// ---------------------------------------------------------------------------

function cmdScan(args: string[]): void {
  let target = ".";
  let generateStubsFlag = false;

  for (const arg of args) {
    if (arg === "--generate-stubs") {
      generateStubsFlag = true;
    } else if (!arg.startsWith("-")) {
      target = arg;
    }
  }

  const files = walkDir(target);

  let totalPoints = 0;
  let autoInstrumentable = 0;
  let needsManual = 0;
  const allPoints: import("./scanner.js").CostPoint[] = [];

  for (const file of files) {
    const source = readFileSync(file, "utf-8");
    const points = scanSource(source, file);

    if (points.length === 0) continue;

    allPoints.push(...points);

    console.log(`\n${file}:`);
    for (const p of points) {
      const tag = p.autoInstrumentable ? "[auto]" : "[manual]";
      console.log(`  L${p.line}: ${tag} ${p.provider} \u2014 ${p.pattern}`);
      totalPoints++;
      if (p.autoInstrumentable) {
        autoInstrumentable++;
      } else {
        needsManual++;
      }
    }
  }

  console.log(`\n--- Summary ---`);
  console.log(`Files scanned: ${files.length}`);
  console.log(`Cost points found: ${totalPoints}`);
  console.log(`  Auto-instrumentable: ${autoInstrumentable}`);
  console.log(`  Needs record_cost(): ${needsManual}`);

  if (generateStubsFlag && allPoints.length > 0) {
    console.log("\nGENERATED STUBS:\n");
    console.log(generateStubs(allPoints, target));
  }
}

// ---------------------------------------------------------------------------
// status command
// ---------------------------------------------------------------------------

function cmdStatus(args: string[]): void {
  // Parse --db path
  let dbPath = join(homedir(), ".dexcost", "buffer.db");
  const dbIdx = args.indexOf("--db");
  if (dbIdx !== -1 && args[dbIdx + 1]) {
    dbPath = args[dbIdx + 1]!;
  }

  console.log(`DB location: ${dbPath}`);

  if (!existsSync(dbPath)) {
    console.log("Status: Database not found");
    return;
  }

  const buffer = new EventBuffer(dbPath);
  try {
    const allEvents = buffer.getAllEvents();
    const allTasks = buffer.getAllTasks();
    const pendingEvents = buffer.getPendingEvents(Number.MAX_SAFE_INTEGER);
    const syncedCount = allEvents.length - pendingEvents.length;

    // Last task started_at
    let lastTask = "none";
    if (allTasks.length > 0) {
      const sorted = allTasks
        .map((t) => t.startedAt)
        .sort((a, b) => b.getTime() - a.getTime());
      lastTask = sorted[0]!.toISOString();
    }

    console.log(`Events: ${allEvents.length}`);
    console.log(`Tasks:  ${allTasks.length}`);
    console.log(`Pending: ${pendingEvents.length}`);
    console.log(`Synced:  ${syncedCount}`);
    console.log(`Last task: ${lastTask}`);

    // Pricing engine version
    const engine = new PricingEngine();
    console.log(`Pricing version: ${engine.pricingVersion}`);

    // Detect optional peer SDKs
    try {
      // eslint-disable-next-line @typescript-eslint/no-require-imports
      const openai = require("openai") as { VERSION?: string };
      console.log(`openai SDK: ${openai.VERSION ?? "(unknown version)"}`);
    } catch {
      console.log("openai SDK: not installed");
    }

    try {
      // eslint-disable-next-line @typescript-eslint/no-require-imports
      const anthropic = require("@anthropic-ai/sdk") as { VERSION?: string };
      console.log(`anthropic SDK: ${anthropic.VERSION ?? "(unknown version)"}`);
    } catch {
      console.log("anthropic SDK: not installed");
    }
  } finally {
    buffer.close();
  }
}

// ---------------------------------------------------------------------------
// rates command
// ---------------------------------------------------------------------------

function cmdRates(args: string[]): void {
  const hasImport = args.includes("--import");
  const hasList = args.includes("--list");
  const hasExport = args.includes("--export");

  if (!hasImport && !hasList && !hasExport) {
    console.log("Usage: dexcost rates [--import path] [--list] [--export path]");
    return;
  }

  const registry = new RateRegistry();

  // Import first (so list/export see the loaded rates)
  if (hasImport) {
    const idx = args.indexOf("--import");
    const importPath = args[idx + 1];
    if (!importPath) {
      console.error("--import requires a file path");
      process.exit(1);
    }
    registry.load(importPath);
    const loadedCount = Object.keys(registry.rates).length;
    console.log(`Loaded ${loadedCount} rate(s) from ${importPath}`);
  }

  if (hasList) {
    const rates = registry.rates;
    const entries = Object.values(rates).sort((a, b) =>
      a.service.localeCompare(b.service)
    );

    if (entries.length === 0) {
      console.log("No rates registered.");
      return;
    }

    // Calculate column widths
    const maxService = Math.max(7, ...entries.map((e) => e.service.length));
    const maxPer = Math.max(3, ...entries.map((e) => e.per.length));

    const header =
      `${"Service".padEnd(maxService)}  ${"Per".padEnd(maxPer)}  Cost USD`;
    const divider = "-".repeat(header.length);

    console.log(header);
    console.log(divider);
    for (const entry of entries) {
      console.log(
        `${entry.service.padEnd(maxService)}  ${entry.per.padEnd(maxPer)}  ${entry.costUsd}`
      );
    }
  }

  if (hasExport) {
    const idx = args.indexOf("--export");
    const exportPath = args[idx + 1];
    if (!exportPath) {
      console.error("--export requires a file path");
      process.exit(1);
    }
    registry.export(exportPath);
    const exportedCount = Object.keys(registry.rates).length;
    console.log(`Exported ${exportedCount} rate(s) to ${exportPath}`);
  }
}

// ---------------------------------------------------------------------------
// Dispatch
// ---------------------------------------------------------------------------

function main(): void {
  const args = process.argv.slice(2);
  const cmd = args[0];

  if (cmd === "scan") {
    cmdScan(args.slice(1));
  } else if (cmd === "status") {
    cmdStatus(args.slice(1));
  } else if (cmd === "rates") {
    cmdRates(args.slice(1));
  } else {
    console.log("Usage: dexcost <command> [options]");
    console.log("  scan [directory]");
    console.log("  status [--db path]");
    console.log("  rates [--import path] [--list] [--export path]");
    process.exit(1);
  }
}

main();
