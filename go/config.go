package dexcost

import (
	"errors"
	"fmt"
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
	// ServiceCatalogURL fetches an external service catalog on init.
	ServiceCatalogURL string `json:"service_catalog_url,omitempty"`

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
// Hardcoded default, overridable only via DEXCOST_ENDPOINT env var.
func (c *Config) resolvedEndpoint() string {
	if env := os.Getenv("DEXCOST_ENDPOINT"); env != "" {
		return env
	}
	return defaultEndpoint
}

func (c *Config) applyDefaults() {
	if c.BatchSize <= 0 {
		c.BatchSize = 100
	}
	if c.FlushIntervalSeconds <= 0 {
		c.FlushIntervalSeconds = 5.0
	}
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
