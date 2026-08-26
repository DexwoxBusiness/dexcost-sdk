import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const typescriptRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");

function readJson(path: string): unknown {
  return JSON.parse(readFileSync(path, "utf8"));
}

describe("release metadata", () => {
  it("locks the optional SQLite runtime to the reviewed Node 20 build", () => {
    const packageJson = readJson(resolve(typescriptRoot, "package.json")) as {
      optionalDependencies?: Record<string, string>;
    };
    const packageLock = readJson(resolve(typescriptRoot, "package-lock.json")) as {
      packages?: Record<string, { version?: string; optionalDependencies?: Record<string, string> }>;
    };

    // better-sqlite3 12.10 removed Node 20 prebuilds. An npm optional-dependency
    // install can otherwise fail silently and leave durable buffering unavailable.
    const reviewedVersion = "12.8.0";
    expect(packageJson.optionalDependencies?.["better-sqlite3"]).toBe(reviewedVersion);
    expect(packageLock.packages?.[""]?.optionalDependencies?.["better-sqlite3"])
      .toBe(reviewedVersion);
    expect(packageLock.packages?.["node_modules/better-sqlite3"]?.version)
      .toBe(reviewedVersion);
  });
});
