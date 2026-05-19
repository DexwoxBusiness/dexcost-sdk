// Package security provides PII redaction and metadata utilities for the dexcost Go SDK.
package security

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
)

// RedactMap returns a shallow copy of data with matching keys replaced by "[REDACTED]".
// Recursively redacts nested maps. Original map is not modified.
func RedactMap(data map[string]interface{}, fields []string) map[string]interface{} {
	fieldSet := make(map[string]struct{}, len(fields))
	for _, f := range fields {
		fieldSet[f] = struct{}{}
	}
	return redactMapWithSet(data, fieldSet)
}

// redactMapWithSet performs the actual recursive redaction using a pre-built field set.
func redactMapWithSet(data map[string]interface{}, fieldSet map[string]struct{}) map[string]interface{} {
	result := make(map[string]interface{}, len(data))
	for k, v := range data {
		if _, redact := fieldSet[k]; redact {
			result[k] = "[REDACTED]"
			continue
		}
		// Recursively handle nested maps.
		if nested, ok := v.(map[string]interface{}); ok {
			result[k] = redactMapWithSet(nested, fieldSet)
		} else {
			result[k] = v
		}
	}
	return result
}

// HashValue returns the SHA-256 hex digest of value.
func HashValue(value string) string {
	sum := sha256.Sum256([]byte(value))
	return hex.EncodeToString(sum[:])
}

// EnforceMetadataLimit returns details unchanged when its JSON-serialized size
// is within maxBytes. When over the limit it returns a deterministic stub
// {"_truncated": true, "_original_size_bytes": N} rather than dropping an
// arbitrary subset of keys (Python parity: redaction.py:39-56).
// If maxBytes is 0, the default of 10240 (10 KB) is used.
func EnforceMetadataLimit(details map[string]interface{}, maxBytes int) map[string]interface{} {
	if maxBytes == 0 {
		maxBytes = 10240
	}

	encoded, err := json.Marshal(details)
	if err != nil {
		return map[string]interface{}{
			"_truncated": true,
			"_error":     "unserializable",
		}
	}
	if len(encoded) <= maxBytes {
		return details
	}
	return map[string]interface{}{
		"_truncated":           true,
		"_original_size_bytes": len(encoded),
	}
}
