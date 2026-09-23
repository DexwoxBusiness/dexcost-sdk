#!/usr/bin/env bash
#
# Active cost-map drift check.
#
# Python and TypeScript are the paired service-catalog implementations and
# must ship a byte-identical LLM cost map. Go and Rust intentionally remain
# independent until catalog parity is adopted for those SDKs; their snapshots
# are therefore not release gates for Python/TypeScript catalog recovery.
#
# Run locally:  scripts/check_cost_map_drift.sh
# CI integration: invoked by .github/workflows/ci.yml on every push/PR.

set -euo pipefail

cd "$(dirname "$0")/.."

PYTHON_PATH=python/src/dexcost/data/model_cost_map.json
TS_PATH=typescript/src/pricing/cost_map.json

if [[ ! -f "$PYTHON_PATH" ]]; then
  echo "FATAL: canonical Python cost map missing: $PYTHON_PATH" >&2
  exit 2
fi

canonical_hash=$(md5sum "$PYTHON_PATH" | cut -d' ' -f1)
canonical_keys=$(python3 -c "import json; print(len(json.load(open('$PYTHON_PATH'))))")

echo "Canonical (Python): hash=$canonical_hash keys=$canonical_keys"

drift=0
for path in "$TS_PATH"; do
  if [[ ! -f "$path" ]]; then
    echo "MISSING: $path" >&2
    drift=1
    continue
  fi
  hash=$(md5sum "$path" | cut -d' ' -f1)
  keys=$(python3 -c "import json; print(len(json.load(open('$path'))))")
  if [[ "$hash" != "$canonical_hash" ]]; then
    echo "DRIFT: $path hash=$hash keys=$keys" >&2
    drift=1
  else
    echo "OK:    $path"
  fi
done

if [[ "$drift" -ne 0 ]]; then
  echo "" >&2
  echo "Active Python/TypeScript cost-map drift detected." >&2
  echo "Review and intentionally realign the paired catalog. Source of truth:" >&2
  echo "  $PYTHON_PATH" >&2
  exit 1
fi

echo "Active Python and TypeScript cost maps are byte-identical."
