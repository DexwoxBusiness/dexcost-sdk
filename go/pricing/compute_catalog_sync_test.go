// Drift check: the bundled Go compute catalog must be byte-identical to
// the Python canonical at python/src/dexcost/data/compute_prices.json.
//
// If this fails, the cross-SDK guarantee breaks — run
// scripts/sync_compute_catalog.sh to regenerate.

package pricing

import (
	"os"
	"path/filepath"
	"testing"
)

func TestComputeCatalogMatchesPythonCanonical(t *testing.T) {
	// Walk up from this test file to find the repo root (must contain
	// both go/ and python/). The test is skipped if the Python sibling
	// isn't reachable (e.g. when running from a vendored Go module).
	repoRoot, ok := findRepoRoot()
	if !ok {
		t.Skip("repo root not reachable from CWD; skipping cross-SDK drift check")
		return
	}

	goPath := filepath.Join(repoRoot, "go", "pricing", "data", "compute_prices.json")
	pyPath := filepath.Join(repoRoot, "python", "src", "dexcost", "data", "compute_prices.json")

	goBytes, err := os.ReadFile(goPath)
	if err != nil {
		t.Fatalf("read go catalog: %v", err)
	}
	pyBytes, err := os.ReadFile(pyPath)
	if err != nil {
		t.Skipf("Python sibling not reachable (%v); skipping drift check", err)
		return
	}
	if string(goBytes) != string(pyBytes) {
		t.Fatalf("compute_prices.json drift detected between Go and Python — " +
			"run: bash scripts/sync_compute_catalog.sh")
	}
}

// findRepoRoot walks parent directories looking for one that contains
// both go/ and python/. Returns the path + true on success.
func findRepoRoot() (string, bool) {
	cwd, err := os.Getwd()
	if err != nil {
		return "", false
	}
	dir := cwd
	for i := 0; i < 8; i++ {
		hasGo := dirExists(filepath.Join(dir, "go"))
		hasPython := dirExists(filepath.Join(dir, "python"))
		if hasGo && hasPython {
			return dir, true
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			return "", false
		}
		dir = parent
	}
	return "", false
}

func dirExists(path string) bool {
	info, err := os.Stat(path)
	return err == nil && info.IsDir()
}
