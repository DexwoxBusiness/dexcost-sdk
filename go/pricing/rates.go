package pricing

import (
	"crypto/sha256"
	"fmt"
	"os"
	"sort"
	"sync"

	"github.com/shopspring/decimal"
	"gopkg.in/yaml.v3"
)

// RateEntry holds a per-unit cost rate for a non-LLM service.
type RateEntry struct {
	Service string
	Per     string
	CostUSD decimal.Decimal
}

// RateRegistry stores per-service cost rates for non-LLM services.
type RateRegistry struct {
	mu      sync.RWMutex
	rates   map[string]*RateEntry
	version string // cached, invalidated on mutation
}

// NewRateRegistry creates an empty RateRegistry.
func NewRateRegistry() *RateRegistry {
	return &RateRegistry{
		rates: make(map[string]*RateEntry),
	}
}

// Register adds or updates a rate for the given service.
func (r *RateRegistry) Register(service, per string, costUSD decimal.Decimal) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.rates[service] = &RateEntry{Service: service, Per: per, CostUSD: costUSD}
	r.version = "" // invalidate cache
}

// Get returns the rate entry for a service, or nil if not found.
func (r *RateRegistry) Get(service string) *RateEntry {
	r.mu.RLock()
	defer r.mu.RUnlock()
	return r.rates[service]
}

// GetAll returns a copy of all registered rates.
func (r *RateRegistry) GetAll() map[string]RateEntry {
	r.mu.RLock()
	defer r.mu.RUnlock()
	result := make(map[string]RateEntry, len(r.rates))
	for k, v := range r.rates {
		result[k] = *v
	}
	return result
}

// rateYAMLInfo is the per-service YAML shape (per + cost_usd), shared by the
// load and export paths.
type rateYAMLInfo struct {
	Per     string `yaml:"per"`
	CostUSD string `yaml:"cost_usd"`
}

// LoadYAML loads rates from a YAML file. The canonical (Python-compatible)
// format nests entries under a top-level `rates:` key:
//
//	rates:
//	  maps.googleapis.com:
//	    per: request
//	    cost_usd: "0.005"
//
// A legacy flat mapping (`service_name: {per, cost_usd}` at the top level) is
// still accepted for backward compatibility.
func (r *RateRegistry) LoadYAML(path string) error {
	data, err := os.ReadFile(path)
	if err != nil {
		return fmt.Errorf("read yaml: %w", err)
	}

	// Canonical format: top-level `rates:` mapping.
	var wrapped struct {
		Rates map[string]rateYAMLInfo `yaml:"rates"`
	}
	if err := yaml.Unmarshal(data, &wrapped); err != nil {
		return fmt.Errorf("parse yaml: %w", err)
	}
	raw := wrapped.Rates
	if raw == nil {
		// Backward compatibility: legacy flat `service: {per, cost_usd}` map.
		if err := yaml.Unmarshal(data, &raw); err != nil {
			return fmt.Errorf("parse yaml: %w", err)
		}
	}

	r.mu.Lock()
	defer r.mu.Unlock()
	for service, entry := range raw {
		cost, err := decimal.NewFromString(entry.CostUSD)
		if err != nil {
			continue // skip malformed entries
		}
		r.rates[service] = &RateEntry{Service: service, Per: entry.Per, CostUSD: cost}
	}
	r.version = ""
	return nil
}

// ExportYAML writes all registered rates to a YAML file in the canonical
// Python-compatible format (entries nested under a top-level `rates:` key).
func (r *RateRegistry) ExportYAML(path string) error {
	r.mu.RLock()
	rates := make(map[string]rateYAMLInfo, len(r.rates))
	for k, v := range r.rates {
		rates[k] = rateYAMLInfo{Per: v.Per, CostUSD: v.CostUSD.String()}
	}
	r.mu.RUnlock()

	data, err := yaml.Marshal(map[string]interface{}{"rates": rates})
	if err != nil {
		return fmt.Errorf("marshal yaml: %w", err)
	}
	return os.WriteFile(path, data, 0644)
}

// PricingVersion returns a deterministic 12-char hex hash of all registered rates.
func (r *RateRegistry) PricingVersion() string {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.version != "" {
		return r.version
	}
	keys := make([]string, 0, len(r.rates))
	for k := range r.rates {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	var raw string
	for i, k := range keys {
		e := r.rates[k]
		if i > 0 {
			raw += "|"
		}
		raw += fmt.Sprintf("%s:%s:%s", e.Service, e.Per, e.CostUSD.String())
	}
	h := sha256.Sum256([]byte(raw))
	r.version = fmt.Sprintf("%x", h[:6])
	return r.version
}
