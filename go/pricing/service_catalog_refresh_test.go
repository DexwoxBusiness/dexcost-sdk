package pricing

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"sync/atomic"
	"testing"
)

func TestBundledServiceCatalogMatchesSafePythonCanonical(t *testing.T) {
	repoRoot, ok := findRepoRoot()
	if !ok {
		t.Skip("repo root not reachable; skipping cross-SDK drift check")
	}
	goData, err := os.ReadFile(filepath.Join(repoRoot, "go", "pricing", "data", "service_prices.json"))
	if err != nil {
		t.Fatalf("read Go catalog: %v", err)
	}
	pythonData, err := os.ReadFile(filepath.Join(repoRoot, "python", "src", "dexcost", "data", "service_prices.json"))
	if err != nil {
		t.Skipf("Python canonical not reachable: %v", err)
	}
	if !bytes.Equal(goData, pythonData) {
		t.Fatal("Go service catalog drifted; run scripts/sync_service_catalog.sh")
	}

	catalog, err := NewServiceCatalog()
	if err != nil {
		t.Fatalf("load bundled catalog: %v", err)
	}
	if len(catalog.Entries()) != 73 {
		t.Fatalf("safe catalog entry count = %d, want 73", len(catalog.Entries()))
	}
}

func remoteCatalogPayload(rate string) map[string]interface{} {
	return map[string]interface{}{
		"data": map[string]interface{}{
			"_meta": map[string]interface{}{
				"version":                "test",
				"service_count":          1,
				"disabled_service_count": 1,
				"safety_policy_version":  "2026-07-14.2",
			},
			"custom_search": map[string]interface{}{
				"display_name":         "Custom Search",
				"domains":              []string{"api.custom-search.test"},
				"category":             "search",
				"pricing_model":        "per_request",
				"cost_per_request_usd": rate,
				"cost_extraction":      map[string]interface{}{"type": "fixed"},
				"source":               "test",
				"last_verified":        "2026-07-14",
			},
		},
		"meta": map[string]interface{}{
			"catalog_version":        "test",
			"safety_policy_version":  "2026-07-14.2",
			"source":                 "bundled",
			"service_count":          1,
			"disabled_service_count": 1,
			"disabled_entries": []map[string]interface{}{
				{"service_key": "unsafe_service"},
			},
		},
	}
}

func TestServiceCatalogRefreshAuthenticatedAtomicReplacement(t *testing.T) {
	var authorization string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		authorization = r.Header.Get("Authorization")
		w.Header().Set("Content-Type", "application/json")
		if err := json.NewEncoder(w).Encode(remoteCatalogPayload("0.01")); err != nil {
			t.Fatalf("encode response: %v", err)
		}
	}))
	defer server.Close()

	catalog, err := NewServiceCatalog()
	if err != nil {
		t.Fatalf("load bundled catalog: %v", err)
	}
	if err := catalog.RefreshFromURL(server.URL, "dx_test_key"); err != nil {
		t.Fatalf("refresh catalog: %v", err)
	}
	if authorization != "Bearer dx_test_key" {
		t.Fatalf("authorization = %q", authorization)
	}
	if catalog.Lookup("https://api.tavily.com/search") != nil {
		t.Fatal("remote refresh must replace, not merge, bundled entries")
	}
	if got := catalog.Lookup("https://api.custom-search.test/search"); got == nil {
		t.Fatal("conformant remote entry was not installed")
	}
}

func TestServiceCatalogRefreshRejectsUnsafeRateWithoutMutation(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(remoteCatalogPayload("0"))
	}))
	defer server.Close()

	catalog, err := NewServiceCatalog()
	if err != nil {
		t.Fatalf("load bundled catalog: %v", err)
	}
	versionBefore := catalog.CatalogVersion()
	if err := catalog.RefreshFromURL(server.URL); err == nil {
		t.Fatal("expected unsafe zero-rate catalog to be rejected")
	}
	if catalog.CatalogVersion() != versionBefore {
		t.Fatal("rejected refresh mutated catalog version")
	}
	if catalog.Lookup("https://api.tavily.com/search") == nil {
		t.Fatal("rejected refresh removed bundled entries")
	}
}

func TestServiceCatalogRefreshRejectsEmptyMap(t *testing.T) {
	payload := remoteCatalogPayload("0.01")
	payload["data"] = map[string]interface{}{
		"_meta": map[string]interface{}{
			"version":                "test",
			"service_count":          0,
			"disabled_service_count": 1,
			"safety_policy_version":  "2026-07-14.2",
		},
	}
	payload["meta"].(map[string]interface{})["service_count"] = 0

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(payload)
	}))
	defer server.Close()

	catalog, err := NewServiceCatalog()
	if err != nil {
		t.Fatalf("load bundled catalog: %v", err)
	}
	if err := catalog.RefreshFromURL(server.URL); err == nil {
		t.Fatal("expected empty catalog to be rejected")
	}
}

func TestServiceCatalogRefreshRejectsUnsupportedSafetyPolicy(t *testing.T) {
	payload := remoteCatalogPayload("0.01")
	payload["meta"].(map[string]interface{})["safety_policy_version"] = "future-policy"
	payload["data"].(map[string]interface{})["_meta"].(map[string]interface{})["safety_policy_version"] = "future-policy"

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(payload)
	}))
	defer server.Close()

	catalog, err := NewServiceCatalog()
	if err != nil {
		t.Fatalf("load bundled catalog: %v", err)
	}
	if err := catalog.RefreshFromURL(server.URL); err == nil {
		t.Fatal("expected unsupported safety policy to be rejected")
	}
	if catalog.Lookup("https://api.tavily.com/search") == nil {
		t.Fatal("rejected refresh mutated the bundled catalog")
	}
}

func TestServiceCatalogRefreshDoesNotFollowRedirects(t *testing.T) {
	var redirectedRequests atomic.Int32
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		redirectedRequests.Add(1)
		_ = json.NewEncoder(w).Encode(remoteCatalogPayload("0.01"))
	}))
	defer target.Close()

	redirect := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, target.URL, http.StatusFound)
	}))
	defer redirect.Close()

	catalog, err := NewServiceCatalog()
	if err != nil {
		t.Fatalf("load bundled catalog: %v", err)
	}
	if err := catalog.RefreshFromURL(redirect.URL, "dx_test_key"); err == nil {
		t.Fatal("expected redirecting catalog endpoint to be rejected")
	}
	if redirectedRequests.Load() != 0 {
		t.Fatal("catalog refresh followed a redirect")
	}
}
