// Package pricing — egress pricing engine.
//
// Resolves a per-GB egress rate from (provider, region) using the bundled
// data/egress_prices.json catalog. Mirrors Python's dexcost.egress_pricing
// faithfully (5-tier degradation ladder, warn-once-per-failure-mode).
//
// Fail-silent contract: every failure mode degrades through the spec §7.1
// ladder; the engine always returns a usable EgressRate.

package pricing

import (
	_ "embed"
	"encoding/json"
	"fmt"
	"log"
	"os"
	"sync"

	"github.com/shopspring/decimal"
)

//go:embed data/egress_prices.json
var embeddedEgressCatalog []byte

// hardcodedEgressDefault is the Tier-4 ultimate fallback — used only when
// the catalog cannot be read at all AND _meta.default_rate_usd_per_gb cannot
// be resolved. Matches the spec §7.1 hardcoded constant.
var hardcodedEgressDefault = decimal.RequireFromString("0.09")

var (
	egressWarnedModes sync.Map // map[string]struct{}
)

// ResetEgressWarningStateForTests clears the warn-once tracking set.
// Test-only helper.
func ResetEgressWarningStateForTests() {
	egressWarnedModes.Range(func(k, _ any) bool {
		egressWarnedModes.Delete(k)
		return true
	})
}

func egressWarnOnce(mode, message string) {
	if _, loaded := egressWarnedModes.LoadOrStore(mode, struct{}{}); loaded {
		return
	}
	log.Printf("WARN dexcost.egress_pricing: %s", message)
}

// EgressRate is the immutable result of an egress-rate lookup.
type EgressRate struct {
	RatePerGB      decimal.Decimal
	PricingSource  string
	CostConfidence string // "exact" | "computed" | "estimated"
}

// EgressPricingEngine resolves egress rates from the bundled catalog.
type EgressPricingEngine struct {
	catalog        map[string]any
	catalogVersion string
}

// NewEgressPricingEngine constructs an engine using the bundled catalog.
func NewEgressPricingEngine() *EgressPricingEngine {
	eng := &EgressPricingEngine{catalogVersion: "unknown"}
	eng.loadBundled()
	return eng
}

// NewEgressPricingEngineFromPath constructs an engine from a specific JSON
// file path. Used by tests to drive Tier-4 fallbacks (missing/malformed).
func NewEgressPricingEngineFromPath(path string) *EgressPricingEngine {
	eng := &EgressPricingEngine{catalogVersion: "unknown"}
	eng.loadPath(path)
	return eng
}

func (e *EgressPricingEngine) loadBundled() {
	e.parse(embeddedEgressCatalog)
}

func (e *EgressPricingEngine) loadPath(path string) {
	raw, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			egressWarnOnce(
				"catalog_missing",
				fmt.Sprintf("egress catalog file not found (%s); falling back to hardcoded default", err),
			)
			return
		}
		egressWarnOnce(
			"catalog_unreadable",
			fmt.Sprintf("egress catalog unreadable (%s); falling back to hardcoded default", err),
		)
		return
	}
	e.parse(raw)
}

func (e *EgressPricingEngine) parse(raw []byte) {
	if len(raw) == 0 {
		egressWarnOnce(
			"catalog_missing",
			"egress catalog file empty; falling back to hardcoded default",
		)
		return
	}
	var parsed map[string]any
	if err := json.Unmarshal(raw, &parsed); err != nil {
		egressWarnOnce(
			"catalog_malformed",
			fmt.Sprintf("egress catalog malformed JSON (%s); falling back to hardcoded default", err),
		)
		e.catalog = nil
		return
	}
	e.catalog = parsed
	if metaAny, ok := parsed["_meta"]; ok {
		if meta, ok := metaAny.(map[string]any); ok {
			if v, ok := meta["version"].(string); ok {
				e.catalogVersion = v
			}
		}
	}
}

// CatalogVersion returns the version string from the catalog's _meta block,
// or "unknown" when the catalog could not be loaded.
func (e *EgressPricingEngine) CatalogVersion() string {
	return e.catalogVersion
}

// RateForInternal returns the rate for traffic classified as internal —
// always free, source="egress_catalog:internal", confidence="exact".
func (e *EgressPricingEngine) RateForInternal() EgressRate {
	return EgressRate{
		RatePerGB:      decimal.Zero,
		PricingSource:  "egress_catalog:internal",
		CostConfidence: "exact",
	}
}

// ResolveRate resolves an egress rate via the §7.1 degradation ladder.
//
// Tier 1: (provider, region) exact match → region rate, "computed".
// Tier 2: provider known, region absent/unknown → provider default, "estimated".
// Tier 3: provider unknown → _meta default, "estimated".
// Tier 4: catalog unreadable or _meta default absent → hardcoded 0.09, "estimated".
func (e *EgressPricingEngine) ResolveRate(provider, region string) EgressRate {
	if provider != "" && e.catalog != nil {
		if blockAny, ok := e.catalog[provider]; ok {
			if block, ok := blockAny.(map[string]any); ok {
				// Tier 1: region exact match
				if region != "" {
					if regionsAny, ok := block["regions"]; ok {
						if regions, ok := regionsAny.(map[string]any); ok {
							if rateAny, ok := regions[region]; ok {
								if rate, ok := decimalFromAny(rateAny); ok {
									return EgressRate{
										RatePerGB:      rate,
										PricingSource:  fmt.Sprintf("egress_catalog:%s:%s", provider, region),
										CostConfidence: "computed",
									}
								}
								egressWarnOnce(
									fmt.Sprintf("region_rate_malformed:%s:%s", provider, region),
									fmt.Sprintf("egress region rate malformed for %s/%s", provider, region),
								)
							}
						}
					}
				}
				// Tier 2: provider default
				if rate, ok := decimalFromAny(block["default_usd_per_gb"]); ok {
					return EgressRate{
						RatePerGB:      rate,
						PricingSource:  fmt.Sprintf("egress_catalog:%s:default", provider),
						CostConfidence: "estimated",
					}
				}
			}
		}
	}

	// Tier 3: _meta default
	if e.catalog != nil {
		if metaAny, ok := e.catalog["_meta"]; ok {
			if meta, ok := metaAny.(map[string]any); ok {
				if rate, ok := decimalFromAny(meta["default_rate_usd_per_gb"]); ok {
					return EgressRate{
						RatePerGB:      rate,
						PricingSource:  "egress_catalog:default",
						CostConfidence: "estimated",
					}
				}
				egressWarnOnce(
					"meta_default_missing",
					"egress _meta.default_rate_usd_per_gb missing/malformed; using hardcoded default",
				)
			}
		}
	}

	// Tier 4: hardcoded
	return EgressRate{
		RatePerGB:      hardcodedEgressDefault,
		PricingSource:  "egress_catalog:default",
		CostConfidence: "estimated",
	}
}

// decimalFromAny attempts to coerce a JSON value (string/number) to decimal.
// Returns (zero, false) when the value is nil or not coercible.
func decimalFromAny(v any) (decimal.Decimal, bool) {
	if v == nil {
		return decimal.Zero, false
	}
	switch x := v.(type) {
	case string:
		if x == "" {
			return decimal.Zero, false
		}
		d, err := decimal.NewFromString(x)
		if err != nil {
			return decimal.Zero, false
		}
		return d, true
	case float64:
		return decimal.NewFromFloat(x), true
	case json.Number:
		d, err := decimal.NewFromString(x.String())
		if err != nil {
			return decimal.Zero, false
		}
		return d, true
	}
	return decimal.Zero, false
}
