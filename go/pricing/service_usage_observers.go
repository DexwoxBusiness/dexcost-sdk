package pricing

// This file intentionally contains no prices. It extracts provider-owned
// usage quantities for services withheld from SDK-side pricing.

import (
	"encoding/json"
	"log"
	"net/url"
	"strings"
	"sync"

	"github.com/shopspring/decimal"
)

type usageObserverDefinition struct {
	ServiceKey                       string                `json:"service_key"`
	ProviderName                     string                `json:"provider_name"`
	ProviderService                  string                `json:"provider_service"`
	Component                        string                `json:"component"`
	Domains                          []string              `json:"domains"`
	Endpoints                        []string              `json:"endpoints"`
	ResponsePath                     string                `json:"response_path"`
	UsageMetric                      string                `json:"usage_metric"`
	ResourceType                     string                `json:"resource_type"`
	ResourcePath                     string                `json:"resource_path"`
	RequestResourcePath              string                `json:"request_resource_path"`
	ResourceQueryParameter           string                `json:"resource_query_parameter"`
	DefaultResourceID                string                `json:"default_resource_id"`
	FixedResourceID                  string                `json:"fixed_resource_id"`
	ResourceVariant                  *usageResourceVariant `json:"resource_variant"`
	QueryAny                         []usageQueryPredicate `json:"query_any"`
	QuantityMultiplierPath           string                `json:"quantity_multiplier_path"`
	QuantityMultiplierQueryParameter string                `json:"quantity_multiplier_query_parameter"`
	RecordIDPath                     string                `json:"record_id_path"`
	RecordIDHeader                   string                `json:"record_id_header"`
	SourceURL                        string                `json:"source_url"`
}

type usageQueryPredicate struct {
	Parameter string `json:"parameter"`
	Operator  string `json:"operator"`
}

type usageResourceVariant struct {
	QueryParameter string `json:"query_parameter"`
	Equals         string `json:"equals"`
	MatchedSuffix  string `json:"matched_suffix"`
	DefaultSuffix  string `json:"default_suffix"`
}

type usageObserverManifest struct {
	Meta struct {
		Version       string `json:"version"`
		ObserverCount int    `json:"observer_count"`
	} `json:"_meta"`
	Observers []usageObserverDefinition `json:"observers"`
}

// ServiceUsageObservation is a canonical provider quantity with no monetary assertion.
type ServiceUsageObservation struct {
	ServiceKey       string
	ProviderName     string
	ProviderService  string
	Component        string
	Metric           string
	Quantity         decimal.Decimal
	ResourceType     string
	ResourceID       string
	ProviderRecordID string
	ManifestVersion  string
}

var (
	usageObserversOnce sync.Once
	usageObservers     usageObserverManifest
	usageObserversOK   bool
)

func loadUsageObservers() {
	data, err := embeddedServiceData.ReadFile("data/service_usage_observers.json")
	if err != nil {
		log.Printf("[dexcost] bundled service usage observers disabled: read manifest: %v", err)
		return
	}
	if err := json.Unmarshal(data, &usageObservers); err != nil {
		log.Printf("[dexcost] bundled service usage observers disabled: invalid JSON: %v", err)
		return
	}
	if usageObservers.Meta.Version == "" ||
		usageObservers.Meta.ObserverCount != len(usageObservers.Observers) {
		log.Printf("[dexcost] bundled service usage observers disabled: inconsistent metadata")
		return
	}
	keys := make(map[string]struct{}, len(usageObservers.Observers))
	for _, observer := range usageObservers.Observers {
		_, duplicate := keys[observer.ServiceKey]
		if duplicate || observer.ServiceKey == "" || observer.ProviderName == "" ||
			observer.ProviderService == "" || observer.ResponsePath == "" ||
			observer.metricInvalid() ||
			(observer.Component != "external" && observer.Component != "speech_to_text") ||
			len(observer.Domains) == 0 || len(observer.Endpoints) == 0 ||
			!allUsageObserverDomainsValid(observer.Domains) ||
			!allUsageObserverEndpointsValid(observer.Endpoints) ||
			!strings.HasPrefix(observer.SourceURL, "https://") {
			log.Printf(
				"[dexcost] bundled service usage observers disabled: invalid observer %q",
				observer.ServiceKey,
			)
			return
		}
		if observer.ResourceType != "" && observer.ResourceType != "model" && observer.ResourceType != "sku" {
			log.Printf("[dexcost] bundled service usage observers disabled: invalid resource type")
			return
		}
		hasResourceSelector := observer.ResourcePath != "" || observer.RequestResourcePath != "" ||
			observer.ResourceQueryParameter != "" || observer.DefaultResourceID != "" ||
			observer.FixedResourceID != ""
		if hasResourceSelector && observer.ResourceType == "" {
			log.Printf("[dexcost] bundled service usage observers disabled: resource selector without type")
			return
		}
		if (observer.QuantityMultiplierPath == "") != (observer.QuantityMultiplierQueryParameter == "") {
			log.Printf("[dexcost] bundled service usage observers disabled: incomplete quantity multiplier")
			return
		}
		if observer.QueryAny != nil && len(observer.QueryAny) == 0 {
			log.Printf("[dexcost] bundled service usage observers disabled: empty query predicate list")
			return
		}
		for _, predicate := range observer.QueryAny {
			if predicate.Parameter == "" || (predicate.Operator != "present" && predicate.Operator != "truthy") {
				log.Printf("[dexcost] bundled service usage observers disabled: invalid query predicate")
				return
			}
		}
		if observer.ResourceVariant != nil && (observer.ResourceVariant.QueryParameter == "" ||
			observer.ResourceVariant.Equals == "" || observer.ResourceVariant.MatchedSuffix == "" ||
			observer.ResourceVariant.DefaultSuffix == "") {
			log.Printf("[dexcost] bundled service usage observers disabled: invalid resource variant")
			return
		}
		keys[observer.ServiceKey] = struct{}{}
	}
	usageObserversOK = true
}

func (observer usageObserverDefinition) metricInvalid() bool {
	return observer.UsageMetric != "input_tokens" && observer.UsageMetric != "audio_seconds"
}

func allUsageObserverDomainsValid(domains []string) bool {
	for _, domain := range domains {
		if strings.TrimSpace(domain) == "" {
			return false
		}
	}
	return true
}

func allUsageObserverEndpointsValid(endpoints []string) bool {
	for _, endpoint := range endpoints {
		if !strings.HasPrefix(endpoint, "/") {
			return false
		}
	}
	return true
}

func usageObserverEndpointMatches(path, endpoint string) bool {
	return path == endpoint || strings.HasPrefix(path, endpoint+"/")
}

func queryValueIsTruthy(value string) bool {
	switch strings.ToLower(strings.TrimSpace(value)) {
	case "", "0", "false", "no", "off":
		return false
	default:
		return true
	}
}

func observerQueryMatches(values url.Values, predicates []usageQueryPredicate) bool {
	if len(predicates) == 0 {
		return true
	}
	for _, predicate := range predicates {
		found, present := values[predicate.Parameter]
		if predicate.Operator == "present" && present {
			return true
		}
		if predicate.Operator == "truthy" {
			for _, value := range found {
				if queryValueIsTruthy(value) {
					return true
				}
			}
		}
	}
	return false
}

func lookupUsageObservers(rawURL string) (*url.URL, []*usageObserverDefinition) {
	usageObserversOnce.Do(loadUsageObservers)
	if !usageObserversOK {
		return nil, nil
	}
	parsed, err := url.Parse(rawURL)
	if err != nil {
		return nil, nil
	}
	matched := make([]*usageObserverDefinition, 0, 1)
	for i := range usageObservers.Observers {
		observer := &usageObservers.Observers[i]
		if !domainMatches(parsed.Hostname(), observer.Domains) {
			continue
		}
		for _, endpoint := range observer.Endpoints {
			if usageObserverEndpointMatches(parsed.Path, endpoint) && observerQueryMatches(parsed.Query(), observer.QueryAny) {
				matched = append(matched, observer)
				break
			}
		}
	}
	return parsed, matched
}

// HasServiceUsageObserver avoids buffering response bodies for unrelated services.
func HasServiceUsageObserver(rawURL string) bool {
	_, observers := lookupUsageObservers(rawURL)
	return len(observers) > 0
}

// ServiceUsageObserverNeedsRequestBody avoids reading request bodies for URL-only observers.
func ServiceUsageObserverNeedsRequestBody(rawURL string) bool {
	_, observers := lookupUsageObservers(rawURL)
	for _, observer := range observers {
		if observer.RequestResourcePath != "" {
			return true
		}
	}
	return false
}

func boundedUsageString(value interface{}) string {
	text, ok := value.(string)
	if !ok {
		return ""
	}
	text = strings.TrimSpace(text)
	if len(text) > 256 {
		return text[:256]
	}
	return text
}

// ObserveServiceUsage extracts a positive quantity from a successful provider response.
func ObserveServiceUsage(rawURL string, headers map[string]string, body map[string]interface{}, requestBody map[string]interface{}) []ServiceUsageObservation {
	parsed, observers := lookupUsageObservers(rawURL)
	if parsed == nil || len(observers) == 0 || body == nil {
		return nil
	}
	result := make([]ServiceUsageObservation, 0, len(observers))
	for _, observer := range observers {
		quantity, ok := toDecimal(resolveDottedPath(body, observer.ResponsePath))
		if !ok || !quantity.IsPositive() {
			continue
		}
		if observer.QuantityMultiplierPath != "" && observer.QuantityMultiplierQueryParameter != "" {
			for _, value := range parsed.Query()[observer.QuantityMultiplierQueryParameter] {
				if queryValueIsTruthy(value) {
					if multiplier, valid := toDecimal(resolveDottedPath(body, observer.QuantityMultiplierPath)); valid && multiplier.IsPositive() {
						quantity = quantity.Mul(multiplier)
					}
					break
				}
			}
		}
		recordID := ""
		if observer.RecordIDPath != "" {
			recordID = boundedUsageString(resolveDottedPath(body, observer.RecordIDPath))
		}
		if recordID == "" && observer.RecordIDHeader != "" {
			for key, value := range headers {
				if strings.EqualFold(key, observer.RecordIDHeader) {
					recordID = boundedUsageString(value)
					break
				}
			}
		}
		resourceID := ""
		if observer.ResourcePath != "" {
			resourceID = boundedUsageString(resolveDottedPath(body, observer.ResourcePath))
		}
		if resourceID == "" && observer.RequestResourcePath != "" {
			resourceID = boundedUsageString(resolveDottedPath(requestBody, observer.RequestResourcePath))
		}
		if resourceID == "" && observer.ResourceQueryParameter != "" {
			resourceID = boundedUsageString(parsed.Query().Get(observer.ResourceQueryParameter))
		}
		if resourceID == "" {
			resourceID = boundedUsageString(observer.FixedResourceID)
		}
		if resourceID == "" {
			resourceID = boundedUsageString(observer.DefaultResourceID)
		}
		if resourceID != "" && observer.ResourceVariant != nil {
			suffix := observer.ResourceVariant.DefaultSuffix
			if parsed.Query().Get(observer.ResourceVariant.QueryParameter) == observer.ResourceVariant.Equals {
				suffix = observer.ResourceVariant.MatchedSuffix
			}
			resourceID = boundedUsageString(resourceID + suffix)
		}
		resourceType := ""
		if resourceID != "" {
			resourceType = observer.ResourceType
		}
		result = append(result, ServiceUsageObservation{
			ServiceKey: observer.ServiceKey, ProviderName: observer.ProviderName,
			ProviderService: observer.ProviderService, Component: observer.Component,
			Metric: observer.UsageMetric, Quantity: quantity, ResourceType: resourceType, ResourceID: resourceID,
			ProviderRecordID: recordID, ManifestVersion: usageObservers.Meta.Version,
		})
	}
	return result
}
