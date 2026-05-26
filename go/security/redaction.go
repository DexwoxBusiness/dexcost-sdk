// Package security provides PII redaction and metadata utilities for the dexcost Go SDK.
package security

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"regexp"
	"strings"
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

// sensitiveQueryParams is the canonical set of query parameter names
// (compared case-insensitively) that ScrubURL strips. Must stay in sync
// with the same set in Python (dexcost/redaction.py), TypeScript
// (src/security/redaction.ts), and Rust (security/redaction.rs).
var sensitiveQueryParams = map[string]struct{}{
	"api_key":              {},
	"apikey":               {},
	"access_token":         {},
	"token":                {},
	"auth":                 {},
	"password":             {},
	"secret":               {},
	"signature":            {},
	"x-amz-signature":      {},
	"x-amz-credential":     {},
	"x-amz-security-token": {},
	"session":              {},
}

var userinfoRegex = regexp.MustCompile(`^(https?://)([^@/?#]+@)?(.+)$`)

var urlInTextRegex = regexp.MustCompile(`https?://[^\s"'<>` + "`" + `]+`)

// ScrubURLsInText runs ScrubURL over every URL found in `text`. Used to
// redact URLs embedded in free-form error messages, exception strings,
// and log lines before they are captured into event details.
//
// The URL matcher accepts `http(s)://` followed by any non-whitespace,
// non-quote, non-bracket character — broad enough to catch real URLs
// without breaking on punctuation that commonly delimits them in prose.
func ScrubURLsInText(text string) string {
	if text == "" {
		return text
	}
	return urlInTextRegex.ReplaceAllStringFunc(text, ScrubURL)
}

// ScrubURL strips credentials from a URL before it is captured into an event.
//
// Removes:
//   - userinfo (`user:pass@`) from the authority
//   - query parameters whose name (case-insensitive) is in the canonical
//     sensitive set OR ends with `-signature`, `-credential`, or
//     `-security-token` (AWS SigV4 surface)
//
// Preserves scheme, host, port, path, non-sensitive query params, and
// fragment. The shape of every removed query parameter is preserved as
// `name=REDACTED` so downstream callers can still see which keys were
// present without leaking the values.
//
// Canonical algorithm — Python/TS/Rust SDK implementations must produce
// byte-identical output for the same input (enforced by
// /fixtures/expected_outputs/security/).
func ScrubURL(url string) string {
	if url == "" {
		return url
	}
	if m := userinfoRegex.FindStringSubmatch(url); m != nil {
		url = m[1] + m[3]
	}

	fragment := ""
	if i := strings.Index(url, "#"); i >= 0 {
		fragment = url[i:]
		url = url[:i]
	}
	qIdx := strings.Index(url, "?")
	if qIdx < 0 {
		return url + fragment
	}
	base := url[:qIdx]
	query := url[qIdx+1:]
	parts := strings.Split(query, "&")
	for i, part := range parts {
		var name string
		if eq := strings.Index(part, "="); eq >= 0 {
			name = part[:eq]
		} else {
			name = part
		}
		lname := strings.ToLower(name)
		_, inSet := sensitiveQueryParams[lname]
		sensitive := inSet ||
			strings.HasSuffix(lname, "-signature") ||
			strings.HasSuffix(lname, "-credential") ||
			strings.HasSuffix(lname, "-security-token")
		if sensitive {
			parts[i] = name + "=REDACTED"
		}
	}
	return base + "?" + strings.Join(parts, "&") + fragment
}
