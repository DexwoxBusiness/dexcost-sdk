package pricing

import (
	"encoding/json"
	"os"
	"reflect"
	"testing"
)

func TestSharedServiceUsageObserverConformance(t *testing.T) {
	raw, err := os.ReadFile("../../fixtures/service_usage_observation_conformance.json")
	if err != nil {
		t.Fatal(err)
	}
	var fixture struct {
		Cases []struct {
			Name     string                   `json:"name"`
			URL      string                   `json:"url"`
			Headers  map[string]string        `json:"headers"`
			Request  map[string]interface{}   `json:"request"`
			Response map[string]interface{}   `json:"response"`
			Expected []map[string]interface{} `json:"expected"`
		} `json:"cases"`
	}
	if err := json.Unmarshal(raw, &fixture); err != nil {
		t.Fatal(err)
	}
	for _, testCase := range fixture.Cases {
		t.Run(testCase.Name, func(t *testing.T) {
			observed := ObserveServiceUsage(testCase.URL, testCase.Headers, testCase.Response, testCase.Request)
			if len(observed) != len(testCase.Expected) {
				t.Fatalf("got %d observations, want %d", len(observed), len(testCase.Expected))
			}
			for index := range observed {
				checks := map[string]string{
					"service_key": observed[index].ServiceKey, "provider_name": observed[index].ProviderName,
					"provider_service": observed[index].ProviderService, "component": observed[index].Component,
					"metric": observed[index].Metric, "quantity": observed[index].Quantity.String(),
					"resource_type": observed[index].ResourceType, "resource_id": observed[index].ResourceID,
					"provider_record_id": observed[index].ProviderRecordID,
				}
				for key, actual := range checks {
					expected, _ := testCase.Expected[index][key].(string)
					if actual != expected {
						t.Fatalf("observation %d %s: got %q want %q", index, key, actual, expected)
					}
				}
			}
		})
	}
}

func TestServiceUsageObserverManifestMatchesCanonical(t *testing.T) {
	canonicalRaw, err := os.ReadFile("../../fixtures/service_usage_observers.json")
	if err != nil {
		t.Fatal(err)
	}
	packagedRaw, err := embeddedServiceData.ReadFile("data/service_usage_observers.json")
	if err != nil {
		t.Fatal(err)
	}
	var canonical, packaged interface{}
	if json.Unmarshal(canonicalRaw, &canonical) != nil || json.Unmarshal(packagedRaw, &packaged) != nil {
		t.Fatal("observer manifests must be valid JSON")
	}
	if !reflect.DeepEqual(packaged, canonical) {
		t.Fatal("packaged observer manifest drifted from canonical")
	}
}

func TestCatalogDoesNotClaimObserverEndpointsByDomainFallback(t *testing.T) {
	catalog, err := NewServiceCatalog()
	if err != nil {
		t.Fatal(err)
	}
	for _, rawURL := range []string{
		"https://api.cohere.com/v2/embed",
		"https://api.jina.ai/v1/embeddings",
	} {
		if entry := catalog.Lookup(rawURL); entry != nil {
			t.Fatalf("%s incorrectly matched priced catalog entry %s", rawURL, entry.Key)
		}
	}
}
