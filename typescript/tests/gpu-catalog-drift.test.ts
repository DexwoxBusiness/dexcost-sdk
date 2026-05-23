/**
 * Cross-SDK gpu_prices.json drift check (TypeScript view).
 *
 * Mirrors python/tests/test_gpu_catalog_sync_consistency.py.
 *
 * Asserts the TypeScript bundle is byte-identical to the canonical Python
 * catalog. Skips gracefully when running from a published npm package
 * where the python/ directory isn't reachable.
 *
 * If this test fails: run scripts/sync_gpu_catalog.sh from repo root.
 */

import { describe, test } from "vitest";
import { readFileSync, existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join, resolve } from "node:path";

const HERE = dirname(fileURLToPath(import.meta.url));

function findRepoRoot(): string | null {
  let cur = resolve(HERE);
  while (cur && cur !== "/" && cur.length > 1) {
    if (
      existsSync(join(cur, "python", "src", "dexcost", "data", "gpu_prices.json")) &&
      existsSync(join(cur, "typescript", "src", "data", "gpu_prices.json"))
    ) {
      return cur;
    }
    const parent = dirname(cur);
    if (parent === cur) break;
    cur = parent;
  }
  return null;
}

describe("GPU catalog drift check", () => {
  test("typescript/src/data/gpu_prices.json byte-equal to python canonical", (ctx) => {
    const root = findRepoRoot();
    if (root === null) {
      ctx.skip();
      return;
    }
    const canonical = join(
      root,
      "python",
      "src",
      "dexcost",
      "data",
      "gpu_prices.json",
    );
    const target = join(root, "typescript", "src", "data", "gpu_prices.json");
    if (!existsSync(canonical) || !existsSync(target)) {
      ctx.skip();
      return;
    }
    const canonicalBytes = readFileSync(canonical);
    const targetBytes = readFileSync(target);
    if (!canonicalBytes.equals(targetBytes)) {
      throw new Error(
        `gpu_prices.json drift detected in TypeScript bundle. ` +
          `Run: bash scripts/sync_gpu_catalog.sh`,
      );
    }
  });
});
