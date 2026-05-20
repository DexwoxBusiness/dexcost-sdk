/**
 * Drift check vs the Python canonical catalog.
 *
 * Pins the cross-SDK invariant: typescript/src/data/compute_prices.json and
 * python/src/dexcost/data/compute_prices.json are byte-identical, refreshed
 * together by scripts/sync_compute_catalog.sh.
 *
 * Skips gracefully when running from a published npm package (where the
 * Python sibling is not present on disk).
 */

import { describe, expect, test } from "vitest";
import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const TS_PATH = join(HERE, "../src/data/compute_prices.json");
// Repo layout: <repo>/typescript/tests/X.test.ts
//              <repo>/python/src/dexcost/data/compute_prices.json
const PY_PATH = join(HERE, "../../python/src/dexcost/data/compute_prices.json");

describe("compute catalog drift", () => {
  test("TS catalog is byte-equal to the Python canonical", () => {
    if (!existsSync(PY_PATH)) {
      // Published-package run — Python sibling not on disk. Skip gracefully.
      // eslint-disable-next-line no-console
      console.warn(
        `[drift-check] Python canonical not found at ${PY_PATH}; skipping byte-equality assertion (this is expected when running from a published npm package).`,
      );
      return;
    }
    const tsBuf = readFileSync(TS_PATH);
    const pyBuf = readFileSync(PY_PATH);
    expect(
      tsBuf.equals(pyBuf),
      `typescript/src/data/compute_prices.json drifted from python/src/dexcost/data/compute_prices.json. ` +
        `Run scripts/sync_compute_catalog.sh to re-sync.`,
    ).toBe(true);
  });
});
