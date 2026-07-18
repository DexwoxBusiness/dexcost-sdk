package pricing

// This file intentionally contains no prices. It extracts provider-owned
// usage quantities for services withheld from SDK-side pricing.

import (
	"encoding/json"
	"net/url"
	"strings"
	"sync"

	"github.com/shopspring/decimal"
)

type usageObserverDefinition struct {
	ServiceKey      string   `json:"service_key"`
	ProviderName    string   `json:"provider_name"`
	ProviderService string   `json:"provider_service"`
	Component       string   `json:"component"`
	Domains         []string `json:"domains"`
	Endpoints       []string `json:"endpoints"`
	ResponsePath    string   `json:"response_path"`
	UsageMetric     string   `json:"usage_metric"`
	ResourcePath    string   `json:"resource_path"`
	RecordIDPath    string   `json:"record_id_path"`
	RecordIDHeader  string   `json:"record_id_header"`
	SourceURL       string   `json:"source_url"`
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
	if err != nil || json.Unmarshal(data, &usageObservers) != nil ||
		usageObservers.Meta.Version == "" ||
		usageObservers.Meta.ObserverCount != len(usageObservers.Observers) {
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

func lookupUsageObserver(rawURL string) *usageObserverDefinition {
	usageObserversOnce.Do(loadUsageObservers)
	if !usageObserversOK {
		return nil
	}
	parsed, err := url.Parse(rawURL)
	if err != nil {
		return nil
	}
	for i := range usageObservers.Observers {
		observer := &usageObservers.Observers[i]
		if !domainMatches(parsed.Hostname(), observer.Domains) {
			continue
		}
		for _, endpoint := range observer.Endpoints {
			if usageObserverEndpointMatches(parsed.Path, endpoint) {
				return observer
			}
		}
	}
	return nil
}

// HasServiceUsageObserver avoids buffering response bodies for unrelated services.
func HasServiceUsageObserver(rawURL string) bool {
	return lookupUsageObserver(rawURL) != nil
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
func ObserveServiceUsage(rawURL string, headers map[string]string, body map[string]interface{}) *ServiceUsageObservation {
	observer := lookupUsageObserver(rawURL)
	if observer == nil || body == nil {
		return nil
	}
	quantity, ok := toDecimal(resolveDottedPath(body, observer.ResponsePath))
	if !ok || !quantity.IsPositive() {
		return nil
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
	return &ServiceUsageObservation{
		ServiceKey: observer.ServiceKey, ProviderName: observer.ProviderName,
		ProviderService: observer.ProviderService, Component: observer.Component,
		Metric: observer.UsageMetric, Quantity: quantity, ResourceID: resourceID,
		ProviderRecordID: recordID, ManifestVersion: usageObservers.Meta.Version,
	}
}
