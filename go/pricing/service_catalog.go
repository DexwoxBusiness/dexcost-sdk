package pricing

import (
	"crypto/sha256"
	"embed"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"strings"
	"sync"

	"github.com/shopspring/decimal"
)

//go:embed data/service_prices.json
var embeddedServiceData embed.FS

// ServiceEntry represents a single service entry from the catalog.
type ServiceEntry struct {
	Key            string
	DisplayName    string
	Domains        []string
	Category       string
	PricingModel   string
	CostExtraction map[string]interface{}
	Source         string
	LastVerified   string
	Endpoints      []string
	RateFields     map[string]interface{}
	Note           string
}

// CostExtractionResult holds the result of extracting cost from an HTTP response.
type CostExtractionResult struct {
	Amount        decimal.Decimal
	Confidence    string
	ServiceName   string
	PricingSource string
}

// ServiceCatalog loads and queries the bundled service price catalog.
type ServiceCatalog struct {
	mu        sync.RWMutex
	entries   map[string]*ServiceEntry
	overrides map[string]*serviceOverride
	rawData   map[string]interface{}
}

type serviceOverride struct {
	CostPerUnit decimal.Decimal
	Per         string
}

// NewServiceCatalog creates a ServiceCatalog from the embedded service_prices.json.
func NewServiceCatalog() (*ServiceCatalog, error) {
	data, err := embeddedServiceData.ReadFile("data/service_prices.json")
	if err != nil {
		return nil, fmt.Errorf("read embedded service catalog: %w", err)
	}
	return newServiceCatalogFromBytes(data)
}

// NewServiceCatalogFromFile creates a ServiceCatalog from an external JSON file.
func NewServiceCatalogFromFile(path string) (*ServiceCatalog, error) {
	data, err := embeddedServiceData.ReadFile(path)
	if err != nil {
		// Fall back to os.ReadFile for absolute paths.
		import_data, err2 := readFileForCatalog(path)
		if err2 != nil {
			return nil, fmt.Errorf("read service catalog file: %w", err2)
		}
		return newServiceCatalogFromBytes(import_data)
	}
	return newServiceCatalogFromBytes(data)
}

// newServiceCatalogFromBytes parses service catalog JSON data.
func newServiceCatalogFromBytes(data []byte) (*ServiceCatalog, error) {
	var raw map[string]json.RawMessage
	if err := json.Unmarshal(data, &raw); err != nil {
		return nil, fmt.Errorf("parse service catalog: %w", err)
	}

	// Also parse as generic map for version hashing.
	var rawData map[string]interface{}
	if err := json.Unmarshal(data, &rawData); err != nil {
		rawData = make(map[string]interface{})
	}

	entries := make(map[string]*ServiceEntry, len(raw))
	for key, entryJSON := range raw {
		if key == "_meta" {
			continue
		}
		entry, err := parseServiceEntry(key, entryJSON)
		if err != nil {
			continue // skip malformed entries
		}
		entries[key] = entry
	}

	return &ServiceCatalog{
		entries:   entries,
		overrides: make(map[string]*serviceOverride),
		rawData:   rawData,
	}, nil
}

// parseServiceEntry parses a single JSON entry into a ServiceEntry.
func parseServiceEntry(key string, data json.RawMessage) (*ServiceEntry, error) {
	var fields map[string]interface{}
	if err := json.Unmarshal(data, &fields); err != nil {
		return nil, err
	}

	entry := &ServiceEntry{
		Key:          key,
		DisplayName:  stringField(fields, "display_name"),
		Category:     stringField(fields, "category"),
		PricingModel: stringField(fields, "pricing_model"),
		Source:       stringField(fields, "source"),
		LastVerified: stringField(fields, "last_verified"),
		Note:         stringField(fields, "note"),
	}

	// Parse domains.
	if domainsRaw, ok := fields["domains"]; ok {
		if arr, ok := domainsRaw.([]interface{}); ok {
			for _, d := range arr {
				if s, ok := d.(string); ok {
					entry.Domains = append(entry.Domains, s)
				}
			}
		}
	}

	// Parse endpoints.
	if endpointsRaw, ok := fields["endpoints"]; ok {
		if arr, ok := endpointsRaw.([]interface{}); ok {
			for _, e := range arr {
				if s, ok := e.(string); ok {
					entry.Endpoints = append(entry.Endpoints, s)
				}
			}
		}
	}

	// Parse cost_extraction.
	if ceRaw, ok := fields["cost_extraction"]; ok {
		if ceMap, ok := ceRaw.(map[string]interface{}); ok {
			entry.CostExtraction = ceMap
		}
	}
	if entry.CostExtraction == nil {
		entry.CostExtraction = make(map[string]interface{})
	}

	// Collect rate fields (all fields not in the standard set).
	standardKeys := map[string]struct{}{
		"display_name":    {},
		"domains":         {},
		"category":        {},
		"pricing_model":   {},
		"cost_extraction": {},
		"source":          {},
		"last_verified":   {},
		"endpoints":       {},
		"note":            {},
	}
	rateFields := make(map[string]interface{})
	for k, v := range fields {
		if _, isStandard := standardKeys[k]; !isStandard {
			rateFields[k] = v
		}
	}
	if len(rateFields) > 0 {
		entry.RateFields = rateFields
	}

	return entry, nil
}

// Lookup matches a URL against the catalog by domain and endpoint.
// Wildcard domains like *.pinecone.io are supported.
// Returns nil if no match is found.
func (sc *ServiceCatalog) Lookup(rawURL string) *ServiceEntry {
	parsed, err := url.Parse(rawURL)
	if err != nil {
		return nil
	}
	hostname := parsed.Hostname()
	path := parsed.Path

	sc.mu.RLock()
	defer sc.mu.RUnlock()

	// Collect all entries whose domains match.
	var candidates []*ServiceEntry
	for _, entry := range sc.entries {
		if domainMatches(hostname, entry.Domains) {
			candidates = append(candidates, entry)
		}
	}

	if len(candidates) == 0 {
		return nil
	}

	// If only one candidate, return it.
	if len(candidates) == 1 {
		return candidates[0]
	}

	// Multiple candidates: filter by endpoint match.
	for _, entry := range candidates {
		if len(entry.Endpoints) > 0 {
			for _, ep := range entry.Endpoints {
				if strings.HasPrefix(path, ep) {
					return entry
				}
			}
		}
	}

	// Fallback: return first candidate without endpoints requirement.
	for _, entry := range candidates {
		if len(entry.Endpoints) == 0 {
			return entry
		}
	}

	// Last resort: first candidate.
	return candidates[0]
}

// domainMatches checks if hostname matches any of the domain patterns.
func domainMatches(hostname string, patterns []string) bool {
	for _, pattern := range patterns {
		if strings.HasPrefix(pattern, "*.") {
			// Wildcard: *.pinecone.io should match
			// "my-index.svc.us-east1-gcp.pinecone.io"
			suffix := pattern[1:] // ".pinecone.io"
			if strings.HasSuffix(hostname, suffix) || hostname == pattern[2:] {
				return true
			}
		} else {
			if hostname == pattern {
				return true
			}
		}
	}
	return false
}

// ExtractCost applies extraction rules to get cost from an HTTP response.
// Returns nil if cost cannot be extracted.
func (sc *ServiceCatalog) ExtractCost(
	entry *ServiceEntry,
	responseHeaders map[string]string,
	responseBody map[string]interface{},
) *CostExtractionResult {
	if entry == nil {
		return nil
	}

	sc.mu.RLock()
	override := sc.overrides[entry.Key]
	sc.mu.RUnlock()

	// Check user override first.
	if override != nil {
		return &CostExtractionResult{
			Amount:        override.CostPerUnit,
			Confidence:    "exact",
			ServiceName:   entry.DisplayName,
			PricingSource: "user_override",
		}
	}

	extraction := entry.CostExtraction
	extType, _ := extraction["type"].(string)
	if extType == "" {
		extType = "fixed"
	}

	switch extType {
	case "response_body":
		return sc.extractFromBody(entry, extraction, responseBody)
	case "response_header":
		return sc.extractFromHeader(entry, extraction, responseHeaders)
	case "endpoint_match":
		return sc.extractEndpointMatch(entry)
	case "fixed":
		return sc.extractFixed(entry)
	default:
		return nil
	}
}

// extractFromBody extracts cost from a response body field.
func (sc *ServiceCatalog) extractFromBody(
	entry *ServiceEntry,
	extraction map[string]interface{},
	body map[string]interface{},
) *CostExtractionResult {
	if body == nil {
		// Use fallback credits if available.
		return sc.tryFallbackCredits(entry, extraction)
	}

	path, _ := extraction["path"].(string)
	value := resolveDottedPath(body, path)
	if value == nil {
		return sc.tryFallbackCredits(entry, extraction)
	}

	rawValue, ok := toDecimal(value)
	if !ok {
		return nil
	}

	// Apply transform if present.
	transform, hasTransform := extraction["transform"].(string)
	if hasTransform {
		rawValue = applyTransform(transform, rawValue, entry)
		return &CostExtractionResult{
			Amount:        rawValue,
			Confidence:    "computed",
			ServiceName:   entry.DisplayName,
			PricingSource: "service_catalog",
		}
	}

	// Multiply by rate.
	rate := getRate(entry)
	if rate != nil {
		rawValue = rawValue.Mul(*rate)
	}

	return &CostExtractionResult{
		Amount:        rawValue,
		Confidence:    "computed",
		ServiceName:   entry.DisplayName,
		PricingSource: "service_catalog",
	}
}

// extractFromHeader extracts cost from a response header.
func (sc *ServiceCatalog) extractFromHeader(
	entry *ServiceEntry,
	extraction map[string]interface{},
	headers map[string]string,
) *CostExtractionResult {
	header, _ := extraction["header"].(string)
	if header == "" {
		return nil
	}

	// Case-insensitive header lookup.
	var headerValue string
	found := false
	headerLower := strings.ToLower(header)
	for k, v := range headers {
		if strings.ToLower(k) == headerLower {
			headerValue = v
			found = true
			break
		}
	}

	if !found {
		return nil
	}

	rawValue, ok := toDecimal(headerValue)
	if !ok {
		return nil
	}

	rate := getRate(entry)
	if rate != nil {
		rawValue = rawValue.Mul(*rate)
	}

	return &CostExtractionResult{
		Amount:        rawValue,
		Confidence:    "computed",
		ServiceName:   entry.DisplayName,
		PricingSource: "service_catalog",
	}
}

// extractEndpointMatch returns a fixed cost per request from endpoint match.
func (sc *ServiceCatalog) extractEndpointMatch(entry *ServiceEntry) *CostExtractionResult {
	cost := getFixedCost(entry)
	if cost == nil {
		return nil
	}
	return &CostExtractionResult{
		Amount:        *cost,
		Confidence:    "exact",
		ServiceName:   entry.DisplayName,
		PricingSource: "service_catalog",
	}
}

// extractFixed returns a fixed cost per request.
func (sc *ServiceCatalog) extractFixed(entry *ServiceEntry) *CostExtractionResult {
	cost := getFixedCost(entry)
	if cost == nil {
		return nil
	}
	return &CostExtractionResult{
		Amount:        *cost,
		Confidence:    "exact",
		ServiceName:   entry.DisplayName,
		PricingSource: "service_catalog",
	}
}

// tryFallbackCredits tries to calculate cost from fallback_credits.
func (sc *ServiceCatalog) tryFallbackCredits(
	entry *ServiceEntry,
	extraction map[string]interface{},
) *CostExtractionResult {
	fallbackRaw, ok := extraction["fallback_credits"]
	if !ok {
		return nil
	}
	fallback, ok := toDecimal(fallbackRaw)
	if !ok {
		return nil
	}
	rate := getRate(entry)
	if rate == nil {
		return nil
	}
	amount := fallback.Mul(*rate)
	return &CostExtractionResult{
		Amount:        amount,
		Confidence:    "estimated",
		ServiceName:   entry.DisplayName,
		PricingSource: "service_catalog",
	}
}

// RegisterOverride registers a user override for a service entry.
// Takes precedence over catalog rates during extraction.
func (sc *ServiceCatalog) RegisterOverride(serviceKey string, costPerUnit decimal.Decimal, per string) {
	if per == "" {
		per = "request"
	}
	sc.mu.Lock()
	defer sc.mu.Unlock()
	sc.overrides[serviceKey] = &serviceOverride{
		CostPerUnit: costPerUnit,
		Per:         per,
	}
}

// RefreshFromURL fetches a service catalog JSON from url and merges its
// entries into the existing catalog. Existing entries are overwritten.
func (sc *ServiceCatalog) RefreshFromURL(rawURL string) error {
	resp, err := http.Get(rawURL) //nolint:gosec // URL is controlled by caller
	if err != nil {
		return fmt.Errorf("fetch catalog: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("fetch catalog: status %d", resp.StatusCode)
	}
	data, err := io.ReadAll(resp.Body)
	if err != nil {
		return fmt.Errorf("read catalog body: %w", err)
	}
	newSc, err := newServiceCatalogFromBytes(data)
	if err != nil {
		return fmt.Errorf("parse catalog: %w", err)
	}
	sc.mu.Lock()
	defer sc.mu.Unlock()
	for k, v := range newSc.entries {
		sc.entries[k] = v
	}
	if newSc.rawData != nil {
		if sc.rawData == nil {
			sc.rawData = make(map[string]interface{})
		}
		for k, v := range newSc.rawData {
			sc.rawData[k] = v
		}
	}
	return nil
}

// CatalogVersion returns a SHA-256 hash (16 hex chars) of the catalog data
// combined with overrides for reproducibility tracking.
func (sc *ServiceCatalog) CatalogVersion() string {
	sc.mu.RLock()
	defer sc.mu.RUnlock()

	content, err := json.Marshal(sc.rawData)
	if err != nil {
		content = []byte("{}")
	}

	// Build override content deterministically.
	overrideMap := make(map[string]interface{}, len(sc.overrides))
	for k, v := range sc.overrides {
		overrideMap[k] = map[string]interface{}{
			"cost_per_unit": v.CostPerUnit.String(),
			"per":           v.Per,
		}
	}
	overrideContent, err := json.Marshal(overrideMap)
	if err != nil {
		overrideContent = []byte("{}")
	}

	combined := append(content, overrideContent...)
	h := sha256.Sum256(combined)
	return fmt.Sprintf("%x", h[:8])
}

// Entries returns a copy of all loaded entries.
func (sc *ServiceCatalog) Entries() map[string]*ServiceEntry {
	sc.mu.RLock()
	defer sc.mu.RUnlock()
	result := make(map[string]*ServiceEntry, len(sc.entries))
	for k, v := range sc.entries {
		result[k] = v
	}
	return result
}

// getRate returns the per-unit rate from the entry's rate fields.
func getRate(entry *ServiceEntry) *decimal.Decimal {
	if entry.RateFields == nil {
		return nil
	}
	for k, v := range entry.RateFields {
		if strings.HasPrefix(k, "cost_per_") && strings.HasSuffix(k, "_usd") {
			d, ok := toDecimal(v)
			if ok {
				return &d
			}
		}
	}
	return nil
}

// getFixedCost returns the fixed cost per request from rate fields.
func getFixedCost(entry *ServiceEntry) *decimal.Decimal {
	if entry.RateFields == nil {
		return nil
	}
	for k, v := range entry.RateFields {
		if strings.HasPrefix(k, "cost_per_") && strings.HasSuffix(k, "_usd") {
			d, ok := toDecimal(v)
			if ok {
				return &d
			}
		}
	}
	return nil
}

// resolveDottedPath resolves a dotted path like "data.stats.computeUnits" in a map.
func resolveDottedPath(data map[string]interface{}, path string) interface{} {
	if path == "" {
		return nil
	}
	parts := strings.Split(path, ".")
	var current interface{} = data
	for _, part := range parts {
		m, ok := current.(map[string]interface{})
		if !ok {
			return nil
		}
		current, ok = m[part]
		if !ok {
			return nil
		}
	}
	return current
}

// applyTransform applies a named transform to a raw value.
func applyTransform(transform string, rawValue decimal.Decimal, entry *ServiceEntry) decimal.Decimal {
	switch transform {
	case "ms_to_seconds":
		seconds := rawValue.Div(decimal.NewFromInt(1000))
		rate := getRate(entry)
		if rate != nil {
			return seconds.Mul(*rate)
		}
		return decimal.Zero
	case "ms_to_minutes":
		minutes := rawValue.Div(decimal.NewFromInt(60000))
		rate := getRate(entry)
		if rate != nil {
			return minutes.Mul(*rate)
		}
		return decimal.Zero
	case "stripe_fee":
		// amount is in cents
		amountDollars := rawValue.Div(decimal.NewFromInt(100))
		return amountDollars.Mul(decimal.RequireFromString("0.029")).Add(decimal.RequireFromString("0.30"))
	default:
		return rawValue
	}
}

// toDecimal converts various types to decimal.Decimal.
func toDecimal(v interface{}) (decimal.Decimal, bool) {
	switch val := v.(type) {
	case float64:
		return decimal.NewFromFloat(val), true
	case int:
		return decimal.NewFromInt(int64(val)), true
	case int64:
		return decimal.NewFromInt(val), true
	case string:
		d, err := decimal.NewFromString(val)
		if err != nil {
			return decimal.Zero, false
		}
		return d, true
	case json.Number:
		d, err := decimal.NewFromString(string(val))
		if err != nil {
			return decimal.Zero, false
		}
		return d, true
	default:
		return decimal.Zero, false
	}
}

// stringField safely extracts a string field from a map.
func stringField(m map[string]interface{}, key string) string {
	if v, ok := m[key]; ok {
		if s, ok := v.(string); ok {
			return s
		}
	}
	return ""
}

// readFileForCatalog reads a file from the OS filesystem.
func readFileForCatalog(path string) ([]byte, error) {
	return os.ReadFile(path)
}
