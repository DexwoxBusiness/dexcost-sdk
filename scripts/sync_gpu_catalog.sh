#!/usr/bin/env bash
# Synchronise the bundled gpu_prices.json across all four SDKs.
#
# Canonical source: python/src/dexcost/data/gpu_prices.json
# Targets:          rust/src/data/, typescript/src/data/, go/pricing/data/
#
# Usage:
#   bash scripts/sync_gpu_catalog.sh            # write (default — local dev)
#   bash scripts/sync_gpu_catalog.sh --check    # exit-non-zero if stale (CI)
#
# Rationale: see conventions §6 (catalog distribution). Each SDK must bundle
# its own local copy because `pip install` / `cargo add` / `npm install` /
# `go get` only ship the SDK's own tarball. A shared file at the repo root
# would be invisible to installed packages. CI runs --check on every PR.
#
# GPU-specific note: per Phase 2 Decision #11 the GPU catalog targets a WEEKLY
# refresh cadence (vs the compute catalog's monthly). The freshness integrity
# test soft-warns at 90 days and hard-fails at 365 days. See
# docs/superpowers/decisions/2026-05-22-gpu-foundation-decisions.md §11.

set -euo pipefail

CANONICAL="python/src/dexcost/data/gpu_prices.json"
TARGETS=(
  "rust/src/data/gpu_prices.json"
  "typescript/src/data/gpu_prices.json"
  "go/pricing/data/gpu_prices.json"
)
MODE="${1:---write}"

if [[ ! -f "$CANONICAL" ]]; then
  echo "::error::canonical file not found: $CANONICAL"
  echo "Run this script from the repo root."
  exit 2
fi

rc=0
for target in "${TARGETS[@]}"; do
  if [[ "$MODE" == "--check" ]]; then
    if [[ ! -f "$target" ]]; then
      echo "::error::$target does not exist (run: bash scripts/sync_gpu_catalog.sh)"
      rc=1
      continue
    fi
    if ! cmp -s "$CANONICAL" "$target"; then
      echo "::error::$target is out of sync with $CANONICAL"
      echo "Run: bash scripts/sync_gpu_catalog.sh"
      rc=1
    fi
  elif [[ "$MODE" == "--write" ]]; then
    mkdir -p "$(dirname "$target")"
    cp "$CANONICAL" "$target"
    echo "synced → $target"
  else
    echo "::error::unknown mode: $MODE (expected --write or --check)"
    exit 2
  fi
done
exit $rc
