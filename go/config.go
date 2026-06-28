package dexcost

import (
	"errors"
	"fmt"
	"log"
	"os"
	"strings"
)

const defaultEndpoint = "https://api.dexcost.io"

// ErrInvalidAPIKey is returned when an API key has an invalid format.
var ErrInvalidAPIKey = errors.New("invalid API key format")

// Config holds the global SDK configuration.
type Config struct {
	APIKey               string  `json:"api_key,omitempty"`
	Storage              string  `json:"storage,omitempty"` // "local" or "" (auto-detect)
	BatchSize            int     `json:"batch_size,omitempty"`
	FlushIntervalSeconds float64 `json:"flush_interval_seconds,omitempty"`
	BufferDir            string  `json:"buffer_dir,omitempty"`

	// RedactFields lists field names to strip from event details before cloud push.
	RedactFields []string `json:"redact_fields,omitempty"`
	// HashCustomerID controls whether customer_id is SHA-256 hashed before cloud push.
	HashCustomerID bool `json:"hash_customer_id,omitempty"`
	// Environment controls the SDK mode. "development" disables cloud push and
	// enables dev console output. Defaults to DEXCOST_ENV env var if empty.
	Environment string `json:"environment,omitempty"`
	// TrackHTTP enables automatic HTTP cost tracking via the service catalog.
	TrackHTTP bool `json:"track_http,omitempty"`

	// Endpoint overrides the Control Layer base URL the SDK pushes telemetry
	// to. Leave empty to use the hardcoded production default
	// (defaultEndpoint). This is the ONLY way to redirect the endpoint — the
	// SDK no longer reads the DEXCOST_ENDPOINT env var, so a hostile process
	// environment cannot exfiltrate telemetry + the Bearer API key. Because
	// the value is developer-supplied and trusted, http:// is accepted here
	// (e.g. http://localhost:3001 for local e2e). Values without an
	// http://|https:// scheme are rejected and fall back to the default.
	Endpoint string `json:"endpoint,omitempty"`
	// ServiceCatalogURL fetches an external service catalog on init.
	ServiceCatalogURL string `json:"service_catalog_url,omitempty"`

	// Sprint 3 Theme F / §4.1.3 (P4): network-event emission knobs,
	// parity with Python `init(network_event_*)`. The HTTP adapter
	// reads these to decide whether a captured call deserves an
	// emitted `network` event (in addition to the always-emitted
	// `external_cost`). Defaults match Python.
	//
	// NetworkEventThresholdBytes: emit when combined request+response
	// bytes exceed this. Default 102_400 (100 KiB), applied by
	// applyDefaults when left at the zero value.
	NetworkEventThresholdBytes int `json:"network_event_threshold_bytes,omitempty"`
	// NetworkEventOnError: emit on response status >= 400. Default true,
	// applied by applyDefaults. (A plain bool can't distinguish an explicit
	// false from the unset zero value, so on-error emission is always on —
	// matching the previous always-on adapter behaviour and Python's default.)
	NetworkEventOnError bool `json:"network_event_on_error,omitempty"`
	// NetworkEventLatencyMs: emit when call latency exceeds this many
	// milliseconds. Default 0 (latency trigger disabled).
	NetworkEventLatencyMs int `json:"network_event_latency_ms,omitempty"`

	// EnableRetryHeuristics turns on the in-memory RetryHeuristicEngine for
	// automatic retry detection. Off by default — without this the engine is
	// unreachable through Init() and only manual MarkRetry tagging works.
	EnableRetryHeuristics bool `json:"enable_retry_heuristics,omitempty"`
	// RetryHeuristicWindow is the sliding-window size in seconds for retry
	// detection. Defaults to 30 when zero.
	RetryHeuristicWindow float64 `json:"retry_heuristic_window,omitempty"`
	// RetryHeuristicThreshold is the confidence threshold in (0,1] for
	// flagging a heuristic retry. Defaults to 0.8 when zero.
	RetryHeuristicThreshold float64 `json:"retry_heuristic_threshold,omitempty"`

	// ComputeBillingOverrides flips per-billing-model defaults at pricing
	// time. Today the only recognised key is "cloud_run":"instance" which
	// switches Cloud Run from the request-based default to instance-based
	// math (Decision #1). Mirrors python init(compute_billing_overrides=).
	ComputeBillingOverrides map[string]string `json:"compute_billing_overrides,omitempty"`

	// K8sNodeAware opts in to the future /api/v1/nodes probe that resolves
	// the underlying node SKU for K8s pods. The probe HTTP call is wired
	// in a later focused task; this flag is plumbed now so callers can
	// future-proof their Init() call.
	K8sNodeAware bool `json:"k8s_node_aware,omitempty"`

	keyType string
}

// ValidateAPIKey checks the key format and returns "live", "test", or ""
// for an empty key. Returns an error for invalid formats.
func ValidateAPIKey(key string) (string, error) {
	if key == "" {
		return "", nil
	}
	if strings.HasPrefix(key, "dx_live_") {
		return "live", nil
	}
	if strings.HasPrefix(key, "dx_test_") {
		return "test", nil
	}
	preview := key
	if len(preview) > 10 {
		preview = preview[:10] + "..."
	}
	return "", fmt.Errorf("%w: key must start with 'dx_live_' or 'dx_test_', got '%s'", ErrInvalidAPIKey, preview)
}

// resolvedEndpoint returns the Control Layer endpoint URL.
//
// The endpoint comes ONLY from explicit in-code configuration via
// Config.Endpoint, defaulting to the hardcoded production URL
// (defaultEndpoint). The SDK deliberately does NOT read the
// DEXCOST_ENDPOINT env var (or any env var) for the endpoint: an attacker
// controlling the process environment (misconfigured CI runner, hostile
// container) could otherwise point it at an HTTP collector and silently
// exfiltrate cost telemetry together with the Bearer API key. Removing the
// env read closes that vector entirely.
//
// Validation is minimal because Config.Endpoint is developer-supplied and
// trusted: a non-empty value must carry an http:// or https:// scheme. The
// explicit field intentionally accepts http:// (e.g. http://localhost for
// e2e) — safe because it is not env-controllable. Anything else is rejected
// with a warning and falls back to the production default.
func (c *Config) resolvedEndpoint() string {
	if c.Endpoint == "" {
		return defaultEndpoint
	}
	if !strings.HasPrefix(c.Endpoint, "http://") && !strings.HasPrefix(c.Endpoint, "https://") {
		log.Printf("dexcost: Config.Endpoint=%q rejected — must start with "+
			"http:// or https://. Falling back to %s.", c.Endpoint, defaultEndpoint)
		return defaultEndpoint
	}
	return c.Endpoint
}

func (c *Config) applyDefaults() {
	if c.BatchSize <= 0 {
		c.BatchSize = 100
	}
	if c.FlushIntervalSeconds <= 0 {
		c.FlushIntervalSeconds = 5.0
	}
	// Network-event emission knobs mirror Python's init() defaults. The
	// struct's zero values can't be told apart from "explicitly set", so the
	// documented defaults are applied when the field is left at its zero value
	// (same convention as BatchSize / FlushIntervalSeconds above).
	if c.NetworkEventThresholdBytes <= 0 {
		c.NetworkEventThresholdBytes = 102_400
	}
	// Python parity: network_event_on_error defaults to true. Without this a
	// bare Config{} would leave it false, contradicting the documented default
	// and the struct comment.
	c.NetworkEventOnError = true
}

func (c *Config) init() error {
	c.applyDefaults()

	// Resolve environment from env var if not set explicitly.
	if c.Environment == "" {
		c.Environment = os.Getenv("DEXCOST_ENV")
	}
	// Enable dev mode for development environment.
	if c.Environment == "development" {
		EnableDevMode()
	}

	if c.APIKey == "" && c.Storage != "local" {
		c.APIKey = os.Getenv("DEXCOST_API_KEY")
	}
	kt, err := ValidateAPIKey(c.APIKey)
	if err != nil {
		return err
	}
	c.keyType = kt
	return nil
}

// StorageMode returns "local" or "cloud" based on configuration.
func (c *Config) StorageMode() string {
	if c.Storage == "local" {
		return "local"
	}
	if c.APIKey != "" {
		return "cloud"
	}
	return "local"
}

// KeyType returns "live", "test", or "".
func (c *Config) KeyType() string {
	return c.keyType
}

// IsSandbox returns true when using a test/sandbox API key.
func (c *Config) IsSandbox() bool {
	return c.keyType == "test"
}

// IsDev returns true when the SDK is in development mode.
func (c *Config) IsDev() bool {
	return c.Environment == "development"
}
