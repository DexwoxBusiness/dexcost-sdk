package security

import (
	"strings"
	"testing"
)

// Test 1: RedactMap removes matching keys.
func TestRedactMap_RemovesMatchingKeys(t *testing.T) {
	data := map[string]interface{}{
		"email":    "user@example.com",
		"name":     "Alice",
		"api_key":  "secret-key-123",
		"project":  "my-project",
	}
	fields := []string{"email", "api_key"}

	result := RedactMap(data, fields)

	if result["email"] != "[REDACTED]" {
		t.Errorf("expected email to be [REDACTED], got %v", result["email"])
	}
	if result["api_key"] != "[REDACTED]" {
		t.Errorf("expected api_key to be [REDACTED], got %v", result["api_key"])
	}
	if result["name"] != "Alice" {
		t.Errorf("expected name to be Alice, got %v", result["name"])
	}
	if result["project"] != "my-project" {
		t.Errorf("expected project to be my-project, got %v", result["project"])
	}
}

// Test 2: RedactMap recursively redacts nested maps.
func TestRedactMap_RecursivelyRedactsNestedMaps(t *testing.T) {
	data := map[string]interface{}{
		"user": map[string]interface{}{
			"email":    "nested@example.com",
			"username": "alice",
		},
		"token": "top-level-token",
	}
	fields := []string{"email", "token"}

	result := RedactMap(data, fields)

	nested, ok := result["user"].(map[string]interface{})
	if !ok {
		t.Fatal("expected user to be a map")
	}
	if nested["email"] != "[REDACTED]" {
		t.Errorf("expected nested email to be [REDACTED], got %v", nested["email"])
	}
	if nested["username"] != "alice" {
		t.Errorf("expected username to be alice, got %v", nested["username"])
	}
	if result["token"] != "[REDACTED]" {
		t.Errorf("expected token to be [REDACTED], got %v", result["token"])
	}
}

// Test 3: RedactMap doesn't modify the original map.
func TestRedactMap_DoesNotModifyOriginal(t *testing.T) {
	data := map[string]interface{}{
		"email": "user@example.com",
		"name":  "Alice",
	}
	fields := []string{"email"}

	_ = RedactMap(data, fields)

	if data["email"] != "user@example.com" {
		t.Errorf("original map was modified: email = %v", data["email"])
	}
}

// Test 4: HashValue returns a 64-char hex string.
func TestHashValue_Returns64CharHex(t *testing.T) {
	result := HashValue("test-value")
	if len(result) != 64 {
		t.Errorf("expected 64 chars, got %d: %s", len(result), result)
	}
}

// Test 5: HashValue is deterministic.
func TestHashValue_IsDeterministic(t *testing.T) {
	a := HashValue("same-input")
	b := HashValue("same-input")
	if a != b {
		t.Errorf("expected same hash for same input, got %s and %s", a, b)
	}

	c := HashValue("different-input")
	if a == c {
		t.Errorf("expected different hashes for different inputs")
	}
}

// Test 6: EnforceMetadataLimit returns unchanged when under limit.
func TestEnforceMetadataLimit_UnchangedWhenUnderLimit(t *testing.T) {
	data := map[string]interface{}{
		"key1": "value1",
		"key2": "value2",
	}
	result := EnforceMetadataLimit(data, 10240)

	if len(result) != 2 {
		t.Errorf("expected 2 keys, got %d", len(result))
	}
	if result["key1"] != "value1" {
		t.Errorf("expected key1=value1, got %v", result["key1"])
	}
}

// Test 7: EnforceMetadataLimit returns a deterministic truncation stub when
// over the limit (Python parity: redaction.py:39-56).
func TestEnforceMetadataLimit_StubWhenOverLimit(t *testing.T) {
	// Build a map that is definitely over 50 bytes.
	data := map[string]interface{}{
		"key1": strings.Repeat("a", 100),
		"key2": strings.Repeat("b", 100),
		"key3": strings.Repeat("c", 100),
	}
	result := EnforceMetadataLimit(data, 50)

	if trunc, ok := result["_truncated"].(bool); !ok || !trunc {
		t.Errorf("expected _truncated=true, got %v", result["_truncated"])
	}
	size, ok := result["_original_size_bytes"].(int)
	if !ok || size <= 50 {
		t.Errorf("expected _original_size_bytes > 50, got %v", result["_original_size_bytes"])
	}
	if len(result) != 2 {
		t.Errorf("expected stub with exactly 2 keys, got %d", len(result))
	}
	// Original payload keys must not leak into the stub.
	for _, k := range []string{"key1", "key2", "key3"} {
		if _, present := result[k]; present {
			t.Errorf("original key %q leaked into truncation stub", k)
		}
	}
}
