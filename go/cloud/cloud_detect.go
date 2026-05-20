// Package cloud — cloud-environment detection for egress pricing.
//
// Phase 1a — env-var detection (sub-millisecond, synchronous).
// Phase 1b — DMI vendor check (~1 ms, Linux-only).
// Phase 2  — background metadata probe (goroutine, ~250 ms budget,
//            never blocks dexcost.Init()).
//
// All env-var names, DMI strings, and IMDS endpoints were verified against
// May-2026 docs. Mirrors python/src/dexcost/cloud_detect.py 1:1.

package cloud

import (
	"context"
	"encoding/json"
	"io"
	"log"
	"net/http"
	"os"
	"regexp"
	"strings"
	"sync"
	"time"
)

// probeTimeout bounds Phase 2 wall time per probe.
const probeTimeout = 250 * time.Millisecond

// dmiFields are read from /sys/class/dmi/id/<field>. Missing files are
// silently skipped (non-Linux hosts have none of these).
var dmiFields = []string{
	"sys_vendor",
	"board_vendor",
	"product_name",
	"chassis_asset_tag",
	"bios_vendor",
	"product_serial",
}

// dmiMatchMode classifies how a DMI rule's needle compares against the
// field value (after case-folding + stripping).
type dmiMatchMode int

const (
	dmiEq dmiMatchMode = iota
	dmiContains
)

// dmiRule defines a single (field, needle, mode, provider) DMI mapping.
type dmiRule struct {
	Field    string
	Needle   string
	Mode     dmiMatchMode
	Provider string
}

// dmiRules is transcribed from cloud-init's ds-identify (canonical) plus
// provider documentation, verified May 2026. Order matters — canonical
// signals (chassis_asset_tag, product_name) MUST precede the sys_vendor
// backups so that the canonical wins on hosts that expose both.
var dmiRules = []dmiRule{
	// Canonical signals first.
	{"chassis_asset_tag", "oraclecloud.com", dmiEq, "oci"},
	{"chassis_asset_tag", "7783-7084-3265-9085-8269-3286-77", dmiEq, "azure"},
	{"product_name", "google compute engine", dmiEq, "gcp"},
	{"product_name", "alibaba cloud ecs", dmiEq, "alibaba"},

	// sys_vendor exact matches.
	{"sys_vendor", "amazon ec2", dmiEq, "aws"},
	{"sys_vendor", "digitalocean", dmiEq, "digitalocean"},
	{"sys_vendor", "hetzner", dmiEq, "hetzner"},
	{"sys_vendor", "vultr", dmiEq, "vultr"},
	{"sys_vendor", "scaleway", dmiEq, "scaleway"},
	{"sys_vendor", "microsoft corporation", dmiEq, "azure"},

	// Looser substring backups — listed last.
	{"sys_vendor", "amazon", dmiContains, "aws"},
	{"sys_vendor", "google", dmiContains, "gcp"},
	{"sys_vendor", "alibaba cloud", dmiContains, "alibaba"},
	{"sys_vendor", "ovh", dmiContains, "ovh"},
}

// CloudEnv is the detected cloud environment.
//
// Source is the audit trail: "env" | "dmi" | "imds" | "none".
// Empty strings (not nil) represent unresolved provider/region.
type CloudEnv struct {
	Provider string
	Region   string
	Source   string
}

// noneEnv is the canonical "undetected" CloudEnv.
var noneEnv = CloudEnv{Provider: "", Region: "", Source: "none"}

var (
	resultMu sync.RWMutex
	result   = noneEnv
)

// GetCloudEnv returns the most recently resolved CloudEnv (may be source="none").
func GetCloudEnv() CloudEnv {
	resultMu.RLock()
	defer resultMu.RUnlock()
	return result
}

// setResult stores the resolved CloudEnv.
func setResult(env CloudEnv) {
	resultMu.Lock()
	defer resultMu.Unlock()
	result = env
}

// ResetForTests resets the package-level state. Test-only helper.
func ResetForTests() {
	resultMu.Lock()
	defer resultMu.Unlock()
	result = noneEnv
}

// SetResultForTests forces the resolved CloudEnv to a known value. Used by
// downstream finalize tests (e.g. tracker.aggregateCosts) that need a
// deterministic egress rate without actually probing IMDS or reading DMI.
func SetResultForTests(env CloudEnv) {
	setResult(env)
}

// readDMIFunc reads the DMI field map. Overridable in tests.
var readDMIFunc = readDMI

// SetDMIReaderForTests overrides the DMI reader. Returns a cleanup function.
func SetDMIReaderForTests(fn func() map[string]string) func() {
	old := readDMIFunc
	readDMIFunc = fn
	return func() { readDMIFunc = old }
}

// -------------------------------------------------------------------------
// Phase 1a — environment variable detection
// -------------------------------------------------------------------------

var azureContainerAppsRegionRE = regexp.MustCompile(`(?i)\.([a-z0-9-]+)\.azurecontainerapps\.io$`)

// azureContainerAppsRegion parses the Azure region out of a Container Apps
// hostname or DNS suffix. Both CONTAINER_APP_HOSTNAME and
// CONTAINER_APP_ENV_DNS_SUFFIX are formatted as
// <...>.<REGION>.azurecontainerapps.io (verified May 2026).
func azureContainerAppsRegion() string {
	for _, v := range []string{"CONTAINER_APP_HOSTNAME", "CONTAINER_APP_ENV_DNS_SUFFIX"} {
		val := os.Getenv(v)
		if val == "" {
			continue
		}
		m := azureContainerAppsRegionRE.FindStringSubmatch(val)
		if len(m) == 2 {
			return strings.ToLower(m[1])
		}
	}
	return ""
}

// anyEnv returns true when any of the listed env vars is non-empty.
func anyEnv(names ...string) bool {
	for _, n := range names {
		if os.Getenv(n) != "" {
			return true
		}
	}
	return false
}

// firstEnv returns the first non-empty env var value among the listed names.
func firstEnv(names ...string) string {
	for _, n := range names {
		if v := os.Getenv(n); v != "" {
			return v
		}
	}
	return ""
}

// detectEnv runs Phase 1a — pure env-var inspection. Returns nil when no
// signal matched.
//
// Detection priority matches Python exactly:
//
//	Modal → RunPod → Render → Railway → Heroku → Koyeb → Fly → Vercel →
//	AWS → Azure → GCP
//
// Earlier matches win (Vercel runs on AWS but surfaces as Vercel; Modal runs
// on AWS/GCP/OCI but surfaces as Modal).
func detectEnv() *CloudEnv {
	// Modal — MODAL_TASK_ID or MODAL_IMAGE_ID; region from MODAL_REGION.
	if anyEnv("MODAL_TASK_ID", "MODAL_IMAGE_ID") {
		return &CloudEnv{Provider: "modal", Region: os.Getenv("MODAL_REGION"), Source: "env"}
	}
	// RunPod — RUNPOD_POD_ID or RUNPOD_POD_HOSTNAME; region from RUNPOD_DC_ID.
	if anyEnv("RUNPOD_POD_ID", "RUNPOD_POD_HOSTNAME") {
		return &CloudEnv{Provider: "runpod", Region: os.Getenv("RUNPOD_DC_ID"), Source: "env"}
	}
	// Render — RENDER or RENDER_SERVICE_ID; no region env var.
	if anyEnv("RENDER", "RENDER_SERVICE_ID") {
		return &CloudEnv{Provider: "render", Region: "", Source: "env"}
	}
	// Railway — RAILWAY_PROJECT_ID or RAILWAY_ENVIRONMENT_ID; region from
	// RAILWAY_REPLICA_REGION (NOT RAILWAY_REGION — that doesn't exist).
	if anyEnv("RAILWAY_PROJECT_ID", "RAILWAY_ENVIRONMENT_ID") {
		return &CloudEnv{Provider: "railway", Region: os.Getenv("RAILWAY_REPLICA_REGION"), Source: "env"}
	}
	// Heroku — DYNO.
	if anyEnv("DYNO") {
		return &CloudEnv{Provider: "heroku", Region: "", Source: "env"}
	}
	// Koyeb — KOYEB_SERVICE_NAME or KOYEB_APP_NAME; region from KOYEB_REGION.
	if anyEnv("KOYEB_SERVICE_NAME", "KOYEB_APP_NAME") {
		return &CloudEnv{Provider: "koyeb", Region: os.Getenv("KOYEB_REGION"), Source: "env"}
	}
	// Fly.io — FLY_REGION or FLY_APP_NAME; region from FLY_REGION.
	if anyEnv("FLY_REGION", "FLY_APP_NAME") {
		return &CloudEnv{Provider: "fly", Region: os.Getenv("FLY_REGION"), Source: "env"}
	}
	// Vercel — VERCEL or VERCEL_REGION; region from VERCEL_REGION.
	if anyEnv("VERCEL", "VERCEL_REGION") {
		return &CloudEnv{Provider: "vercel", Region: os.Getenv("VERCEL_REGION"), Source: "env"}
	}
	// AWS — Lambda / Execution-Env / ECS signals are definitive; bare
	// AWS_REGION is also accepted.
	if anyEnv(
		"AWS_LAMBDA_FUNCTION_NAME",
		"AWS_EXECUTION_ENV",
		"ECS_CONTAINER_METADATA_URI_V4",
		"ECS_CONTAINER_METADATA_URI",
		"AWS_REGION",
		"AWS_DEFAULT_REGION",
	) {
		region := firstEnv("AWS_REGION", "AWS_DEFAULT_REGION")
		return &CloudEnv{Provider: "aws", Region: region, Source: "env"}
	}
	// Azure — WEBSITE_SITE_NAME / FUNCTIONS_WORKER_RUNTIME / CONTAINER_APP_NAME;
	// region from REGION_NAME, falling back to parsed Container Apps hostname.
	if anyEnv("WEBSITE_SITE_NAME", "FUNCTIONS_WORKER_RUNTIME", "CONTAINER_APP_NAME") {
		region := os.Getenv("REGION_NAME")
		if region == "" {
			region = azureContainerAppsRegion()
		}
		return &CloudEnv{Provider: "azure", Region: region, Source: "env"}
	}
	// GCP — K_SERVICE / K_CONFIGURATION / GAE_ENV / FUNCTION_TARGET /
	// FUNCTION_NAME. No region env var (resolved via Phase 2).
	if anyEnv("K_SERVICE", "K_CONFIGURATION", "GAE_ENV", "FUNCTION_TARGET", "FUNCTION_NAME") {
		return &CloudEnv{Provider: "gcp", Region: "", Source: "env"}
	}
	return nil
}

// -------------------------------------------------------------------------
// Phase 1b — DMI check
// -------------------------------------------------------------------------

// readDMI reads all DMI fields we care about. Missing files are silently
// skipped (non-Linux hosts have none of these).
func readDMI() map[string]string {
	out := make(map[string]string, len(dmiFields))
	for _, f := range dmiFields {
		raw, err := os.ReadFile("/sys/class/dmi/id/" + f)
		if err != nil {
			continue
		}
		out[f] = strings.ToLower(strings.TrimSpace(string(raw)))
	}
	return out
}

// detectDMI resolves the cloud provider from DMI fields. Rules are ordered
// from most specific to most generic; the first match wins.
func detectDMI() *CloudEnv {
	dmi := readDMIFunc()
	for _, r := range dmiRules {
		val := dmi[r.Field]
		if val == "" {
			continue
		}
		switch r.Mode {
		case dmiEq:
			if val == r.Needle {
				return &CloudEnv{Provider: r.Provider, Region: "", Source: "dmi"}
			}
		case dmiContains:
			if strings.Contains(val, r.Needle) {
				return &CloudEnv{Provider: r.Provider, Region: "", Source: "dmi"}
			}
		}
	}
	return nil
}

// -------------------------------------------------------------------------
// Phase 2 — metadata probes
// -------------------------------------------------------------------------

// gcpPathToRegion strips a GCP metadata-server response to a bare region.
//
// Both /instance/zone (projects/<num>/zones/us-central1-a) and
// /instance/region (projects/<num>/regions/us-central1) use the same
// projects/.../X/<name> shape. dropZoneLetter strips the trailing -<letter>
// from the zone form to yield a region.
func gcpPathToRegion(value string, dropZoneLetter bool) string {
	if value == "" {
		return ""
	}
	if idx := strings.LastIndex(value, "/"); idx >= 0 {
		value = value[idx+1:]
	}
	if value == "" {
		return ""
	}
	if dropZoneLetter {
		idx := strings.LastIndex(value, "-")
		if idx < 0 {
			return ""
		}
		return value[:idx]
	}
	return value
}

// httpGetWithCtx issues an HTTP request with the given timeout.
// Returns the response body (bytes) on 2xx, or an error.
func httpGetWithCtx(method, url string, headers map[string]string) ([]byte, error) {
	ctx, cancel := context.WithTimeout(context.Background(), probeTimeout)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, method, url, nil)
	if err != nil {
		return nil, err
	}
	for k, v := range headers {
		req.Header.Set(k, v)
	}
	client := &http.Client{Timeout: probeTimeout}
	resp, err := client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return nil, &probeError{msg: "non-2xx status"}
	}
	return io.ReadAll(resp.Body)
}

type probeError struct{ msg string }

func (e *probeError) Error() string { return e.msg }

// probeFunc is the canonical Phase-2 probe signature.
type probeFunc func() *CloudEnv

func probeAWS() *CloudEnv {
	tokenBytes, err := httpGetWithCtx(
		http.MethodPut,
		"http://169.254.169.254/latest/api/token",
		map[string]string{"X-aws-ec2-metadata-token-ttl-seconds": "21600"},
	)
	if err != nil {
		return nil
	}
	token := strings.TrimSpace(string(tokenBytes))
	regionBytes, err := httpGetWithCtx(
		http.MethodGet,
		"http://169.254.169.254/latest/meta-data/placement/region",
		map[string]string{"X-aws-ec2-metadata-token": token},
	)
	if err != nil {
		return nil
	}
	return &CloudEnv{Provider: "aws", Region: strings.TrimSpace(string(regionBytes)), Source: "imds"}
}

func probeGCP() *CloudEnv {
	// Prefer /region (Cloud Run / Cloud Functions Gen2 return placeholder on /zone).
	headers := map[string]string{"Metadata-Flavor": "Google"}
	if body, err := httpGetWithCtx(
		http.MethodGet,
		"http://metadata.google.internal/computeMetadata/v1/instance/region",
		headers,
	); err == nil {
		region := gcpPathToRegion(strings.TrimSpace(string(body)), false)
		if region != "" {
			return &CloudEnv{Provider: "gcp", Region: region, Source: "imds"}
		}
	}
	// Fall back to /zone for older GCE images.
	body, err := httpGetWithCtx(
		http.MethodGet,
		"http://metadata.google.internal/computeMetadata/v1/instance/zone",
		headers,
	)
	if err != nil {
		return nil
	}
	return &CloudEnv{
		Provider: "gcp",
		Region:   gcpPathToRegion(strings.TrimSpace(string(body)), true),
		Source:   "imds",
	}
}

func probeAzure() *CloudEnv {
	body, err := httpGetWithCtx(
		http.MethodGet,
		"http://169.254.169.254/metadata/instance?api-version=2021-02-01",
		map[string]string{"Metadata": "true"},
	)
	if err != nil {
		return nil
	}
	var payload struct {
		Compute struct {
			Location string `json:"location"`
		} `json:"compute"`
	}
	if err := json.Unmarshal(body, &payload); err != nil {
		return nil
	}
	return &CloudEnv{Provider: "azure", Region: payload.Compute.Location, Source: "imds"}
}

func probeOCI() *CloudEnv {
	// OCI IMDSv2 — use /canonicalRegionName to get the full identifier
	// (us-phoenix-1) — /region returns abbreviated codes (phx).
	body, err := httpGetWithCtx(
		http.MethodGet,
		"http://169.254.169.254/opc/v2/instance/canonicalRegionName",
		map[string]string{"Authorization": "Bearer Oracle"},
	)
	if err != nil {
		return nil
	}
	return &CloudEnv{
		Provider: "oci",
		Region:   strings.ToLower(strings.TrimSpace(string(body))),
		Source:   "imds",
	}
}

func probeDigitalOcean() *CloudEnv {
	body, err := httpGetWithCtx(
		http.MethodGet,
		"http://169.254.169.254/metadata/v1/region",
		nil,
	)
	if err != nil {
		return nil
	}
	return &CloudEnv{
		Provider: "digitalocean",
		Region:   strings.ToLower(strings.TrimSpace(string(body))),
		Source:   "imds",
	}
}

func probeAlibaba() *CloudEnv {
	// Alibaba ECS metadata at 100.100.100.200 — different IP.
	body, err := httpGetWithCtx(
		http.MethodGet,
		"http://100.100.100.200/latest/meta-data/region-id",
		nil,
	)
	if err != nil {
		return nil
	}
	return &CloudEnv{
		Provider: "alibaba",
		Region:   strings.ToLower(strings.TrimSpace(string(body))),
		Source:   "imds",
	}
}

// probes maps provider name → metadata probe. Mutable so tests can inject
// fakes via WithProbeOverrideForTests.
var probes = map[string]probeFunc{
	"aws":          probeAWS,
	"gcp":          probeGCP,
	"azure":        probeAzure,
	"oci":          probeOCI,
	"digitalocean": probeDigitalOcean,
	"alibaba":      probeAlibaba,
}

// fanoutProbes is the major-3 set probed in parallel when no DMI hint is
// available. Adding OCI/DO/Alibaba would lengthen worst-case wait and hit
// wrong endpoints (DO shares AWS's IP); they only run when DMI pre-classifies.
var fanoutProbes = []string{"aws", "gcp", "azure"}

// FanoutProbesForTests exposes fanoutProbes to tests for the
// "fanout limited to aws/gcp/azure" assertion.
func FanoutProbesForTests() []string {
	out := make([]string, len(fanoutProbes))
	copy(out, fanoutProbes)
	return out
}

// WithProbeOverrideForTests replaces a single provider's probe. Returns a
// cleanup function that restores the original.
func WithProbeOverrideForTests(name string, fn probeFunc) func() {
	old, hadOld := probes[name]
	probes[name] = fn
	return func() {
		if hadOld {
			probes[name] = old
		} else {
			delete(probes, name)
		}
	}
}

// runProbe runs Phase 2 probes; returns the first success or {"none"}.
func runProbe(providerHint string) CloudEnv {
	if providerHint != "" {
		if fn, ok := probes[providerHint]; ok {
			if env := fn(); env != nil {
				return *env
			}
			return CloudEnv{Provider: providerHint, Region: "", Source: "imds"}
		}
	}

	type result struct{ env CloudEnv }
	ch := make(chan result, len(fanoutProbes))
	for _, name := range fanoutProbes {
		fn := probes[name]
		if fn == nil {
			continue
		}
		go func(fn probeFunc) {
			if env := fn(); env != nil {
				ch <- result{env: *env}
			} else {
				ch <- result{}
			}
		}(fn)
	}
	deadline := time.After(probeTimeout + 50*time.Millisecond)
	pending := len(fanoutProbes)
	for pending > 0 {
		select {
		case r := <-ch:
			if r.env.Provider != "" {
				return r.env
			}
			pending--
		case <-deadline:
			return noneEnv
		}
	}
	return noneEnv
}

// -------------------------------------------------------------------------
// Orchestration
// -------------------------------------------------------------------------

// DetectNow runs Phase 1a + 1b synchronously. Used by tests; never calls IMDS.
func DetectNow() CloudEnv {
	env := detectEnv()
	if env != nil && env.Provider != "" && env.Region != "" {
		return *env
	}
	dmi := detectDMI()
	if env == nil {
		if dmi != nil {
			return *dmi
		}
		return noneEnv
	}
	return *env
}

var bgGoroutineMu sync.Mutex
var bgGoroutineRunning bool

// IsBackgroundGoroutineRunningForTests reports whether the Phase 2 goroutine
// is currently in flight. Test-only.
func IsBackgroundGoroutineRunningForTests() bool {
	bgGoroutineMu.Lock()
	defer bgGoroutineMu.Unlock()
	return bgGoroutineRunning
}

// StartBackgroundDetection runs Phase 1a + 1b synchronously (sub-millisecond),
// then launches Phase 2 in a goroutine if needed. Returns immediately.
//
// When trackNetwork is false, no probe is launched and the result is set to
// the "none" sentinel.
func StartBackgroundDetection(trackNetwork bool) {
	if !trackNetwork {
		setResult(noneEnv)
		return
	}

	initial := DetectNow()
	setResult(initial)
	if initial.Provider != "" && initial.Region != "" {
		return
	}

	bgGoroutineMu.Lock()
	if bgGoroutineRunning {
		bgGoroutineMu.Unlock()
		return
	}
	bgGoroutineRunning = true
	bgGoroutineMu.Unlock()

	go func() {
		defer func() {
			bgGoroutineMu.Lock()
			bgGoroutineRunning = false
			bgGoroutineMu.Unlock()
		}()
		defer func() {
			if r := recover(); r != nil {
				log.Printf("WARN dexcost.cloud: background probe panicked: %v", r)
			}
		}()
		env := runProbe(initial.Provider)
		if env.Provider != "" {
			// Preserve the env-derived region when the probe didn't supply one.
			if initial.Region != "" && env.Region == "" {
				env.Region = initial.Region
			}
			setResult(env)
		}
	}()
}
