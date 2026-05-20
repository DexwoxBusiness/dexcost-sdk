#!/usr/bin/env bash
# Synchronise the bundled egress_prices.json across all four SDKs.
#
# Canonical source: python/src/dexcost/data/egress_prices.json
# Targets:          rust/src/data/, typescript/src/data/, go/pricing/data/
#
# Usage:
#   bash scripts/sync_egress_catalog.sh            # write (default — local dev)
#   bash scripts/sync_egress_catalog.sh --check    # exit-non-zero if stale (CI)
#
# Rationale: see docs/superpowers/plans/2026-05-20-network-capture-go-rust-ts.md
# §0a Decision #7 — each SDK must bundle its own local copy because
# `pip install` / `cargo add` / `npm install` / `go get` only ship the SDK's
# own tarball. A shared file at the repo root would be invisible to installed
# packages. CI runs --check on every PR to guard against drift.

set -euo pipefail

CANONICAL="python/src/dexcost/data/egress_prices.json"
TARGETS=(
  "rust/src/data/egress_prices.json"
  "typescript/src/data/egress_prices.json"
  "go/pricing/data/egress_prices.json"
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
      echo "::error::$target does not exist (run: bash scripts/sync_egress_catalog.sh)"
      rc=1
      continue
    fi
    if ! cmp -s "$CANONICAL" "$target"; then
      echo "::error::$target is out of sync with $CANONICAL"
      echo "Run: bash scripts/sync_egress_catalog.sh"
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
