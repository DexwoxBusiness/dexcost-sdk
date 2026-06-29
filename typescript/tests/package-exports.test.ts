/**
 * Guards the dual ESM/CJS packaging contract.
 *
 * Issue: the package shipped ESM-only (`exports` had no `require` entry), so
 * CommonJS consumers (NestJS and most production Node.js apps) could not
 * `require('@dexcost/sdk')` and had to fall back to async `import()`. The
 * package now ships a parallel CJS build and advertises both conditions.
 * If any of these fields regress, CJS consumers silently break again — hence
 * this manifest-level guard.
 */

import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const pkg = JSON.parse(readFileSync(join(here, "..", "package.json"), "utf-8"));

describe("package.json dual-format exports", () => {
  it("advertises both import (ESM) and require (CJS) conditions", () => {
    const root = pkg.exports?.["."];
    expect(root).toBeDefined();
    expect(root.import).toBe("./dist/index.js");
    expect(root.require).toBe("./dist/cjs/index.js");
    expect(root.types).toBe("./dist/index.d.ts");
  });

  it("`main` points at the CJS entry for require()-by-default tooling", () => {
    expect(pkg.main).toBe("./dist/cjs/index.js");
  });

  it("`module` points at the ESM entry for bundlers", () => {
    expect(pkg.module).toBe("./dist/index.js");
  });

  it("build script produces the CJS tree", () => {
    expect(pkg.scripts.build).toContain("build-cjs");
  });
});
