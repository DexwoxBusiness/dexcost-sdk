#!/usr/bin/env bash
# Synchronise the bundled service_prices.json safety-filtered baseline across
# all four SDKs.
#
# Canonical source: python/src/dexcost/data/service_prices.json
# Targets:          rust/src/data/, typescript/src/data/, go/pricing/data/
#
# The canonical file is the output of the control-plane safety policy, not the
# unfiltered source catalog. Runtime refreshes may replace it only after the
# authenticated control-plane envelope passes SDK conformance validation.
#
# Usage:
#   bash scripts/sync_service_catalog.sh
#   bash scripts/sync_service_catalog.sh --check

set -euo pipefail

CANONICAL="python/src/dexcost/data/service_prices.json"
TARGETS=(
  "rust/src/data/service_prices.json"
  "typescript/src/data/service_prices.json"
  "go/pricing/data/service_prices.json"
)
MODE="${1:---write}"

if [[ ! -f "$CANONICAL" ]]; then
  echo "::error::canonical file not found: $CANONICAL"
  echo "Run this script from the repository root."
  exit 2
fi

rc=0
for target in "${TARGETS[@]}"; do
  if [[ "$MODE" == "--check" ]]; then
    if [[ ! -f "$target" ]]; then
      echo "::error::$target does not exist (run: bash scripts/sync_service_catalog.sh)"
      rc=1
      continue
    fi
    if ! cmp -s "$CANONICAL" "$target"; then
      echo "::error::$target is out of sync with $CANONICAL"
      echo "Run: bash scripts/sync_service_catalog.sh"
      rc=1
    fi
  elif [[ "$MODE" == "--write" ]]; then
    mkdir -p "$(dirname "$target")"
    cp "$CANONICAL" "$target"
    echo "synced -> $target"
  else
    echo "::error::unknown mode: $MODE (expected --write or --check)"
    exit 2
  fi
done
exit $rc
