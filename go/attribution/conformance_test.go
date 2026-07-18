package attribution

import (
	"encoding/json"
	"os"
	"testing"
)

func TestSharedAttributionV2Conformance(t *testing.T) {
	raw, err := os.ReadFile("../../fixtures/attribution_v2/conformance.json")
	if err != nil {
		t.Fatal(err)
	}
	var fixture struct {
		ContractVersion string `json:"contract_version"`
		Valid           []struct {
			Name  string                 `json:"name"`
			Event map[string]interface{} `json:"event"`
		} `json:"valid"`
		Invalid []struct {
			Name         string                 `json:"name"`
			ExpectedPath string                 `json:"expected_error_path"`
			Event        map[string]interface{} `json:"event"`
		} `json:"invalid"`
	}
	if err := json.Unmarshal(raw, &fixture); err != nil {
		t.Fatal(err)
	}
	if fixture.ContractVersion != ContractVersion {
		t.Fatalf("fixture version %s != %s", fixture.ContractVersion, ContractVersion)
	}
	names := make(map[string]struct{}, len(fixture.Valid)+len(fixture.Invalid))
	for _, test := range fixture.Valid {
		if _, exists := names[test.Name]; exists {
			t.Fatalf("duplicate conformance case name %q", test.Name)
		}
		names[test.Name] = struct{}{}
	}
	for _, test := range fixture.Invalid {
		if _, exists := names[test.Name]; exists {
			t.Fatalf("duplicate conformance case name %q", test.Name)
		}
		names[test.Name] = struct{}{}
	}
	for _, test := range fixture.Valid {
		t.Run("valid/"+test.Name, func(t *testing.T) {
			result := ValidateEventV2(test.Event)
			if !result.Success {
				t.Fatalf("unexpected issues: %+v", result.Issues)
			}
		})
	}
	for _, test := range fixture.Invalid {
		t.Run("invalid/"+test.Name, func(t *testing.T) {
			result := ValidateEventV2(test.Event)
			if result.Success {
				t.Fatal("expected validation failure")
			}
			for _, issue := range result.Issues {
				if issue.Path == test.ExpectedPath {
					return
				}
			}
			t.Fatalf("missing expected path %q in %+v", test.ExpectedPath, result.Issues)
		})
	}
}

func TestValidateEventV2RejectsNormalizedCalendarDate(t *testing.T) {
	event := map[string]interface{}{
		"schema_version": "2", "event_id": "11111111-1111-1111-1111-111111111111", "task_id": "22222222-2222-2222-2222-222222222222",
		"occurred_at": "2026-02-29T10:00:00Z", "observed_at": "2026-04-31T10:00:00Z", "component": "external",
		"provider": map[string]interface{}{"name": "test", "service": "api"}, "lifecycle": map[string]interface{}{"state": "final", "revision": 1},
		"usage": []interface{}{map[string]interface{}{"metric": "request_count", "quantity": "1", "unit": "Requests"}},
	}
	result := ValidateEventV2(event)
	if result.Success {
		t.Fatal("impossible calendar dates must be rejected")
	}
}
