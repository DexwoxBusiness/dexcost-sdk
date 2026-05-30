// Copies non-TS runtime assets (JSON pricing/schema catalogs) from src/ into
// dist/, preserving directory structure. `tsc` does NOT emit .json files, but
// the SDK loads several at runtime via readFileSync()/require() (e.g.
// src/data/service_prices.json, src/pricing/cost_map.json, src/schema/*.json).
// Without this step a `tsc`-only build ships a broken package.
import { readdirSync, mkdirSync, copyFileSync, statSync } from "node:fs";
import { join, dirname, relative } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const srcRoot = join(here, "..", "src");
const distRoot = join(here, "..", "dist");

const EXT = new Set([".json"]);

function walk(dir) {
  const out = [];
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) {
      out.push(...walk(full));
    } else if (EXT.has(full.slice(full.lastIndexOf(".")))) {
      out.push(full);
    }
  }
  return out;
}

let count = 0;
for (const file of walk(srcRoot)) {
  const rel = relative(srcRoot, file);
  const dest = join(distRoot, rel);
  mkdirSync(dirname(dest), { recursive: true });
  copyFileSync(file, dest);
  count++;
}
console.log(`copy-assets: copied ${count} asset file(s) into dist/`);
