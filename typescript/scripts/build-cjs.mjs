// Builds a CommonJS copy of the SDK into dist/cjs/ so the package works with
// `require()` (NestJS, most production Node.js apps compile to CommonJS).
//
// Why a transform pass and NOT a single bundle:
//   - Several modules load JSON assets at runtime relative to their own file
//     location (e.g. `join(dirname(fileURLToPath(import.meta.url)), "cost_map.json")`).
//     Bundling collapses every module into one file, which breaks those
//     per-module relative paths. A non-bundled, structure-preserving transform
//     keeps each module's own `__filename`, so asset resolution still works.
//   - `import.meta.url` is invalid in CJS. esbuild leaves it empty, which would
//     break `fileURLToPath(import.meta.url)` and `createRequire(import.meta.url)`.
//     We shim it via a per-file banner that recreates the value from `__filename`
//     and a `--define` so every `import.meta.url` reference points at the shim.
//
// The ESM build (plain `tsc`) and type declarations live in dist/ as before;
// this script only produces the parallel CJS tree in dist/cjs/.
import { readdirSync, statSync, mkdirSync, writeFileSync, copyFileSync } from "node:fs";
import { join, dirname, relative } from "node:path";
import { fileURLToPath } from "node:url";
import { build } from "esbuild";

const here = dirname(fileURLToPath(import.meta.url));
const pkgRoot = join(here, "..");
const srcRoot = join(pkgRoot, "src");
const outRoot = join(pkgRoot, "dist", "cjs");

/** Recursively collect files under `dir` whose extension is in `exts`. */
function walk(dir, exts) {
  const out = [];
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) {
      out.push(...walk(full, exts));
    } else if (exts.has(full.slice(full.lastIndexOf(".")))) {
      out.push(full);
    }
  }
  return out;
}

const tsFiles = walk(srcRoot, new Set([".ts"]));

await build({
  entryPoints: tsFiles,
  outdir: outRoot,
  outbase: srcRoot,
  format: "cjs",
  platform: "node",
  target: "node18",
  // No bundling — transform each module independently so relative imports and
  // per-module asset paths survive (see header comment).
  bundle: false,
  sourcemap: true,
  logLevel: "warning",
  // Recreate `import.meta.url` from the CJS `__filename` of *this* output file.
  banner: {
    js: "const __dexcost_import_meta_url = require('url').pathToFileURL(__filename).href;",
  },
  define: {
    "import.meta.url": "__dexcost_import_meta_url",
  },
});

// Copy runtime JSON assets into the CJS tree (same reason as copy-assets.mjs:
// tsc/esbuild don't emit .json, but modules readFileSync/require them).
let assetCount = 0;
for (const file of walk(srcRoot, new Set([".json"]))) {
  const dest = join(outRoot, relative(srcRoot, file));
  mkdirSync(dirname(dest), { recursive: true });
  copyFileSync(file, dest);
  assetCount++;
}

// Mark the CJS tree as CommonJS. The root package.json is `"type": "module"`,
// so without this marker Node would treat dist/cjs/*.js as ESM and the
// `require()` of the package would fail. A nested package.json scopes the
// module type to this directory only.
writeFileSync(join(outRoot, "package.json"), JSON.stringify({ type: "commonjs" }, null, 2) + "\n");

console.log(`build-cjs: emitted CJS build (${tsFiles.length} modules, ${assetCount} assets) into dist/cjs/`);
