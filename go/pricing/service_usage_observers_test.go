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
			Name     string                 `json:"name"`
			URL      string                 `json:"url"`
			Headers  map[string]string      `json:"headers"`
			Response map[string]interface{} `json:"response"`
			Expected map[string]interface{} `json:"expected"`
		} `json:"cases"`
	}
	if err := json.Unmarshal(raw, &fixture); err != nil {
		t.Fatal(err)
	}
	for _, testCase := range fixture.Cases {
		t.Run(testCase.Name, func(t *testing.T) {
			observed := ObserveServiceUsage(testCase.URL, testCase.Headers, testCase.Response)
			if testCase.Expected == nil {
				if observed != nil {
					t.Fatalf("unexpected observation: %+v", observed)
				}
				return
			}
			if observed == nil {
				t.Fatal("expected an observation")
			}
			checks := map[string]string{
				"service_key": observed.ServiceKey, "provider_name": observed.ProviderName,
				"provider_service": observed.ProviderService, "component": observed.Component,
				"metric": observed.Metric, "quantity": observed.Quantity.String(),
				"resource_id": observed.ResourceID, "provider_record_id": observed.ProviderRecordID,
			}
			for key, actual := range checks {
				expected, _ := testCase.Expected[key].(string)
				if actual != expected {
					t.Fatalf("%s: got %q want %q", key, actual, expected)
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
