#!/usr/bin/env bash
#
# P2 drift check — Sprint 3 Theme F / plan §4.1.2.
#
# Asserts that all four SDKs ship a byte-identical LLM cost map.
# Pre-fix the TS cost_map.json was 708 keys behind Python+Go and the
# Rust copy was 1 key ahead. Exit code 1 if any MD5 differs from the
# Python canonical.
#
# Run locally:  scripts/check_cost_map_drift.sh
# CI integration: invoked by .github/workflows/cross-sdk-drift.yml
#                 on every push/PR.

set -euo pipefail

cd "$(dirname "$0")/.."

PYTHON_PATH=python/src/dexcost/data/model_cost_map.json
GO_PATH=go/pricing/data/model_cost_map.json
TS_PATH=typescript/src/pricing/cost_map.json
RUST_PATH=rust/src/pricing/cost_map.json

if [[ ! -f "$PYTHON_PATH" ]]; then
  echo "FATAL: canonical Python cost map missing: $PYTHON_PATH" >&2
  exit 2
fi

canonical_hash=$(md5sum "$PYTHON_PATH" | cut -d' ' -f1)
canonical_keys=$(python3 -c "import json; print(len(json.load(open('$PYTHON_PATH'))))")

echo "Canonical (Python): hash=$canonical_hash keys=$canonical_keys"

drift=0
for path in "$GO_PATH" "$TS_PATH" "$RUST_PATH"; do
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
  echo "P2 drift detected. To realign, copy the Python canonical to" >&2
  echo "the diverging SDK and commit. Source of truth:" >&2
  echo "  $PYTHON_PATH" >&2
  exit 1
fi

echo "All four cost maps are byte-identical."
