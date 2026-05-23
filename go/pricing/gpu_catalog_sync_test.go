// Drift check: the bundled Go GPU catalog must be byte-identical to the
// Python canonical at python/src/dexcost/data/gpu_prices.json.
//
// If this fails, the cross-SDK guarantee breaks — run
// scripts/sync_gpu_catalog.sh (when present) or copy the Python file in
// place to regenerate.
//
// Mirrors python commit d7d48b6 conventions update — every SDK ships the
// SAME catalog so cross-SDK Control Layer aggregation works without
// per-language pricing surprises.

package pricing

import (
	"os"
	"path/filepath"
	"testing"
)

func TestGPUCatalogMatchesPythonCanonical(t *testing.T) {
	repoRoot, ok := findRepoRoot()
	if !ok {
		t.Skip("repo root not reachable from CWD; skipping cross-SDK drift check")
		return
	}

	goPath := filepath.Join(repoRoot, "go", "pricing", "data", "gpu_prices.json")
	pyPath := filepath.Join(repoRoot, "python", "src", "dexcost", "data", "gpu_prices.json")

	goBytes, err := os.ReadFile(goPath)
	if err != nil {
		t.Fatalf("read go gpu catalog: %v", err)
	}
	pyBytes, err := os.ReadFile(pyPath)
	if err != nil {
		t.Skipf("Python sibling not reachable (%v); skipping drift check", err)
		return
	}
	if string(goBytes) != string(pyBytes) {
		t.Fatalf("gpu_prices.json drift detected between Go and Python — " +
			"the Go SDK must ship the Python canonical byte-for-byte. " +
			"Sync the catalog or run the sync script.")
	}
}
