package pricing

import (
	"crypto/sha256"
	"embed"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"regexp"
	"strings"
	"sync"
	"time"

	"github.com/DexwoxBusiness/dexcost-sdk/go/internal/safego"
	"github.com/shopspring/decimal"
)

//go:embed data/model_cost_map.json
var embeddedData embed.FS

// CostResult holds the computed cost for a model invocation.
type CostResult struct {
	CostUSD        decimal.Decimal
	CostConfidence string
	PricingSource  string
	PricingVersion string
}

// modelPricing holds per-token costs parsed from the bundled JSON.
type modelPricing struct {
	InputCostPerToken  decimal.Decimal
	OutputCostPerToken decimal.Decimal
	CacheReadCost      decimal.Decimal
	HasCacheRead       bool
	// CacheCreationCost is the Anthropic-specific rate for tokens written to
	// the prompt cache. Charged separately from cache reads and normal input.
	CacheCreationCost decimal.Decimal
	HasCacheCreation  bool
	// Anthropic usage reports input, cache-read, and cache-write as disjoint
	// buckets. OpenAI reports cached tokens as a subset of input tokens.
	CacheTokensAreDisjoint bool
}

// customPricing holds per-1k-token costs set by the user.
type customPricing struct {
	InputPer1k  decimal.Decimal
	OutputPer1k decimal.Decimal
}

// Engine provides LLM cost lookups from bundled pricing data.
// Thread-safe via sync.RWMutex on custom pricing.
type Engine struct {
	models         map[string]*modelPricing
	custom         map[string]*customPricing
	mu             sync.RWMutex
	pricingVersion string
	stopCh         chan struct{}
}

// NewEngine loads the embedded model_cost_map.json bundled at compile time.
func NewEngine() (*Engine, error) {
	data, err := embeddedData.ReadFile("data/model_cost_map.json")
	if err != nil {
		return nil, fmt.Errorf("read embedded pricing data: %w", err)
	}
	return newEngineFromBytes(data)
}

// NewEngineFromFile loads model pricing from an external JSON file.
func NewEngineFromFile(path string) (*Engine, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read pricing file: %w", err)
	}
	return newEngineFromBytes(data)
}

func newEngineFromBytes(data []byte) (*Engine, error) {
	var raw map[string]json.RawMessage
	if err := json.Unmarshal(data, &raw); err != nil {
		return nil, fmt.Errorf("parse pricing data: %w", err)
	}

	models := make(map[string]*modelPricing, len(raw))
	for name, entry := range raw {
		var fields map[string]interface{}
		if err := json.Unmarshal(entry, &fields); err != nil {
			continue
		}
		mp := &modelPricing{}
		if v, ok := fields["input_cost_per_token"]; ok {
			mp.InputCostPerToken = decimalFromInterface(v)
		}
		if v, ok := fields["output_cost_per_token"]; ok {
			mp.OutputCostPerToken = decimalFromInterface(v)
		}
		if v, ok := fields["cache_read_input_token_cost"]; ok {
			mp.CacheReadCost = decimalFromInterface(v)
			mp.HasCacheRead = true
		}
		if v, ok := fields["cache_creation_input_token_cost"]; ok {
			mp.CacheCreationCost = decimalFromInterface(v)
			mp.HasCacheCreation = true
		}
		provider, _ := fields["litellm_provider"].(string)
		mp.CacheTokensAreDisjoint = usesDisjointCacheBuckets(name, provider)
		models[name] = mp
	}

	// Compute pricing version as 12-char SHA-256 prefix of the raw data.
	h := sha256.Sum256(data)
	version := fmt.Sprintf("%x", h[:6])

	return &Engine{
		models:         models,
		custom:         make(map[string]*customPricing),
		pricingVersion: version,
	}, nil
}

// dateSuffixRe matches date suffixes like -2024-08-06 at end of model name.
var dateSuffixRe = regexp.MustCompile(`-\d{4}-\d{2}-\d{2}$`)

func usesDisjointCacheBuckets(model, provider string) bool {
	provider = strings.ToLower(provider)
	model = strings.ToLower(model)
	return provider == "anthropic" ||
		provider == "vertex_ai-anthropic_models" ||
		strings.Contains(model, "claude") ||
		strings.Contains(model, "anthropic.")
}

// GetCost computes the cost for a model invocation given token counts.
// It tries: exact match, provider-prefix stripped, date-suffix fallback.
//
// cachedTokens are prompt-cache *read* tokens (discounted); cacheCreationTokens
// are prompt-cache *write* tokens. Anthropic reports both as disjoint from
// inputTokens, while OpenAI includes cachedTokens inside inputTokens.
func (e *Engine) GetCost(model string, inputTokens, outputTokens, cachedTokens, cacheCreationTokens int) CostResult {
	// Check custom pricing first.
	e.mu.RLock()
	cp, hasCustom := e.custom[model]
	e.mu.RUnlock()

	if hasCustom {
		thousand := decimal.NewFromInt(1000)
		inputTokens = max(inputTokens, 0)
		outputTokens = max(outputTokens, 0)
		cachedTokens = max(cachedTokens, 0)
		cacheCreationTokens = max(cacheCreationTokens, 0)
		hasUnpricedDisjointCache := usesDisjointCacheBuckets(model, "") &&
			(cachedTokens > 0 || cacheCreationTokens > 0)
		billableInput := inputTokens
		confidence := "computed"
		if hasUnpricedDisjointCache {
			billableInput += cachedTokens + cacheCreationTokens
			confidence = "unknown"
		}
		inputCost := cp.InputPer1k.Mul(decimal.NewFromInt(int64(billableInput))).Div(thousand)
		outputCost := cp.OutputPer1k.Mul(decimal.NewFromInt(int64(outputTokens))).Div(thousand)
		return CostResult{
			CostUSD:        inputCost.Add(outputCost),
			CostConfidence: confidence,
			PricingSource:  "custom",
			PricingVersion: e.pricingVersion,
		}
	}

	// Try exact match.
	mp := e.findModel(model)
	if mp == nil {
		return CostResult{
			CostUSD:        decimal.Zero,
			CostConfidence: "unknown",
			PricingSource:  "unknown",
			PricingVersion: e.pricingVersion,
		}
	}

	return e.computeCost(mp, inputTokens, outputTokens, cachedTokens, cacheCreationTokens)
}

// findModel resolves a model name to its pricing entry using fallback strategies.
func (e *Engine) findModel(model string) *modelPricing {
	// 1. Exact match.
	if mp, ok := e.models[model]; ok {
		return mp
	}

	// 2. Strip provider prefix (e.g. "openai/gpt-4o" -> "gpt-4o").
	for i := 0; i < len(model); i++ {
		if model[i] == '/' {
			stripped := model[i+1:]
			if mp, ok := e.models[stripped]; ok {
				return mp
			}
			break
		}
	}

	// 3. Date suffix fallback: strip trailing -YYYY-MM-DD and retry.
	base := dateSuffixRe.ReplaceAllString(model, "")
	if base != model {
		if mp, ok := e.models[base]; ok {
			return mp
		}
	}

	return nil
}

func (e *Engine) computeCost(mp *modelPricing, inputTokens, outputTokens, cachedTokens, cacheCreationTokens int) CostResult {
	if inputTokens < 0 {
		inputTokens = 0
	}
	if outputTokens < 0 {
		outputTokens = 0
	}
	if cachedTokens < 0 {
		cachedTokens = 0
	}
	if cacheCreationTokens < 0 {
		cacheCreationTokens = 0
	}

	confidence := "computed"
	var totalCost decimal.Decimal
	if mp.CacheTokensAreDisjoint {
		cacheReadRate := mp.CacheReadCost
		cacheCreationRate := mp.CacheCreationCost
		if cachedTokens > 0 && !mp.HasCacheRead {
			cacheReadRate = mp.InputCostPerToken
			confidence = "unknown"
		}
		if cacheCreationTokens > 0 && !mp.HasCacheCreation {
			cacheCreationRate = mp.InputCostPerToken
			confidence = "unknown"
		}
		totalCost = mp.InputCostPerToken.Mul(decimal.NewFromInt(int64(inputTokens))).
			Add(cacheReadRate.Mul(decimal.NewFromInt(int64(cachedTokens)))).
			Add(cacheCreationRate.Mul(decimal.NewFromInt(int64(cacheCreationTokens)))).
			Add(mp.OutputCostPerToken.Mul(decimal.NewFromInt(int64(outputTokens))))
	} else {
		effectiveCached := 0
		if mp.HasCacheRead {
			effectiveCached = cachedTokens
			if effectiveCached > inputTokens {
				effectiveCached = inputTokens
			}
		}
		remaining := inputTokens - effectiveCached
		effectiveCreation := 0
		if mp.HasCacheCreation {
			effectiveCreation = cacheCreationTokens
			if effectiveCreation > remaining {
				effectiveCreation = remaining
			}
		}
		nonCachedInput := remaining - effectiveCreation
		totalCost = mp.InputCostPerToken.Mul(decimal.NewFromInt(int64(nonCachedInput))).
			Add(mp.CacheReadCost.Mul(decimal.NewFromInt(int64(effectiveCached)))).
			Add(mp.CacheCreationCost.Mul(decimal.NewFromInt(int64(effectiveCreation)))).
			Add(mp.OutputCostPerToken.Mul(decimal.NewFromInt(int64(outputTokens))))
	}

	return CostResult{
		CostUSD:        totalCost,
		CostConfidence: confidence,
		PricingSource:  "litellm",
		PricingVersion: e.pricingVersion,
	}
}

// SetCustomPricing overrides bundled pricing for a model with per-1k-token rates.
func (e *Engine) SetCustomPricing(model string, inputPer1k, outputPer1k decimal.Decimal) {
	e.mu.Lock()
	defer e.mu.Unlock()
	e.custom[model] = &customPricing{
		InputPer1k:  inputPer1k,
		OutputPer1k: outputPer1k,
	}
}

// PricingVersion returns the 12-char SHA-256 prefix of the pricing data.
func (e *Engine) PricingVersion() string {
	return e.pricingVersion
}

// ModelCount returns the number of models in the bundled pricing data.
func (e *Engine) ModelCount() int {
	return len(e.models)
}

// serverPricingResponse is the JSON shape returned by the pricing server.
type serverPricingResponse struct {
	Models map[string]struct {
		InputCostPerToken  float64 `json:"input_cost_per_token"`
		OutputCostPerToken float64 `json:"output_cost_per_token"`
		CacheReadCost      float64 `json:"cache_read_input_token_cost"`
		CacheCreationCost  float64 `json:"cache_creation_input_token_cost"`
	} `json:"models"`
}

// RefreshFromServer fetches the latest pricing data from the given endpoint,
// parses it, and atomically replaces the engine's model map and version.
// The endpoint should be the server base URL (e.g. "https://example.com").
// Returns an error if the HTTP request or JSON parsing fails; on error the
// existing pricing data is left unchanged.
func (e *Engine) RefreshFromServer(endpoint string) error {
	url := strings.TrimRight(endpoint, "/") + "/v1/api/pricing-data/latest"
	resp, err := http.Get(url) //nolint:gosec // URL is controlled by caller
	if err != nil {
		return fmt.Errorf("pricing refresh GET: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("pricing refresh: unexpected status %d", resp.StatusCode)
	}

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return fmt.Errorf("pricing refresh read body: %w", err)
	}

	var payload serverPricingResponse
	if err := json.Unmarshal(body, &payload); err != nil {
		return fmt.Errorf("pricing refresh parse JSON: %w", err)
	}

	models := make(map[string]*modelPricing, len(payload.Models))
	for name, entry := range payload.Models {
		mp := &modelPricing{
			InputCostPerToken:  decimal.NewFromFloat(entry.InputCostPerToken),
			OutputCostPerToken: decimal.NewFromFloat(entry.OutputCostPerToken),
		}
		if entry.CacheReadCost != 0 {
			mp.CacheReadCost = decimal.NewFromFloat(entry.CacheReadCost)
			mp.HasCacheRead = true
		}
		if entry.CacheCreationCost != 0 {
			mp.CacheCreationCost = decimal.NewFromFloat(entry.CacheCreationCost)
			mp.HasCacheCreation = true
		}
		models[name] = mp
	}

	// Recompute version from the raw response body.
	h := sha256.Sum256(body)
	version := fmt.Sprintf("%x", h[:6])

	e.mu.Lock()
	e.models = models
	e.pricingVersion = version
	e.mu.Unlock()

	return nil
}

// StartBackgroundRefresh immediately refreshes pricing (errors ignored) and
// then continues refreshing on every interval tick until StopBackgroundRefresh
// is called. Calling StartBackgroundRefresh while one is already running is a
// no-op.
func (e *Engine) StartBackgroundRefresh(endpoint string, interval time.Duration) {
	e.mu.Lock()
	if e.stopCh != nil {
		// Already running.
		e.mu.Unlock()
		return
	}
	ch := make(chan struct{})
	e.stopCh = ch
	e.mu.Unlock()

	// Initial refresh before the first tick.
	_ = e.RefreshFromServer(endpoint) //nolint:errcheck

	// safego.Go wraps a `defer recover()` so a panic in the background
	// refresh (network teardown, malformed cost map mid-parse, etc.) is
	// logged but cannot crash the customer's process.
	// Sprint 1 Theme B / §2.2.5.
	safego.Go("pricing-refresh", func() {
		ticker := time.NewTicker(interval)
		defer ticker.Stop()
		for {
			select {
			case <-ticker.C:
				_ = e.RefreshFromServer(endpoint) //nolint:errcheck
			case <-ch:
				return
			}
		}
	})
}

// StopBackgroundRefresh stops the background refresh goroutine if one is
// running. Safe to call multiple times.
func (e *Engine) StopBackgroundRefresh() {
	e.mu.Lock()
	defer e.mu.Unlock()
	if e.stopCh != nil {
		close(e.stopCh)
		e.stopCh = nil
	}
}

// decimalFromInterface converts a JSON number to decimal.Decimal without
// going through float64 string formatting issues. It handles both float64
// and json.Number values.
func decimalFromInterface(v interface{}) decimal.Decimal {
	switch val := v.(type) {
	case float64:
		return decimal.NewFromFloat(val)
	case string:
		d, _ := decimal.NewFromString(val)
		return d
	default:
		return decimal.Zero
	}
}
