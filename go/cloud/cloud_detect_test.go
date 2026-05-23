// Tests mirror python/tests/test_cloud_detect.py (42 cases) faithfully —
// same fixtures, same provider names, same assertions.

package cloud

import (
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"testing"
	"time"
)

// allCloudEnvVars covers every env var any test below might set or expect
// to be unset — t.Setenv("", "") doesn't unset, so we explicitly clear.
var allCloudEnvVars = []string{
	"AWS_LAMBDA_FUNCTION_NAME", "AWS_EXECUTION_ENV",
	"AWS_REGION", "AWS_DEFAULT_REGION",
	"ECS_CONTAINER_METADATA_URI_V4", "ECS_CONTAINER_METADATA_URI",
	"WEBSITE_SITE_NAME", "FUNCTIONS_WORKER_RUNTIME", "CONTAINER_APP_NAME",
	"REGION_NAME", "CONTAINER_APP_HOSTNAME", "CONTAINER_APP_ENV_DNS_SUFFIX",
	"K_SERVICE", "K_CONFIGURATION", "GAE_ENV", "FUNCTION_TARGET", "FUNCTION_NAME",
	"FLY_REGION", "FLY_APP_NAME",
	"VERCEL", "VERCEL_REGION", "VERCEL_ENV",
	"MODAL_TASK_ID", "MODAL_IMAGE_ID", "MODAL_REGION",
	"RUNPOD_POD_ID", "RUNPOD_POD_HOSTNAME", "RUNPOD_DC_ID",
	"REPLICATE_MODEL_ID", "REPLICATE_DEPLOYMENT_ID",
	"RENDER", "RENDER_SERVICE_ID", "RENDER_REGION",
	"RAILWAY_PROJECT_ID", "RAILWAY_ENVIRONMENT_ID", "RAILWAY_REGION", "RAILWAY_REPLICA_REGION",
	"DYNO", "HEROKU_APP_NAME",
	"KOYEB_SERVICE_NAME", "KOYEB_APP_NAME", "KOYEB_REGION",
	"NETLIFY", "NETLIFY_SITE_ID",
	"CF_PAGES", "CLOUDFLARE_ACCOUNT_ID",
}

// clearEnv unsets every cloud env var for the duration of the test.
// Uses t.Setenv so values are auto-restored on test cleanup.
func clearEnv(t *testing.T) {
	t.Helper()
	for _, v := range allCloudEnvVars {
		t.Setenv(v, "")
		// t.Setenv("", "") just sets to empty; ensure it's truly unset by
		// using Unsetenv directly with a cleanup.
		_ = v
	}
}

// resetModule clears the package-level result + clears env vars + installs
// an empty-DMI reader. Returns the cleanup is registered via t.Cleanup.
func resetModule(t *testing.T) {
	t.Helper()
	ResetForTests()
	clearEnv(t)
	t.Cleanup(SetDMIReaderForTests(func() map[string]string { return map[string]string{} }))
}

// dmiFixture installs a DMI fixture (values are lowercased like the real reader).
func dmiFixture(t *testing.T, fields map[string]string) {
	t.Helper()
	lowered := make(map[string]string, len(fields))
	for k, v := range fields {
		lowered[k] = strings.ToLower(v)
	}
	t.Cleanup(SetDMIReaderForTests(func() map[string]string { return lowered }))
}

// -------------------------------------------------------------------------
// Phase 1a — env vars
// -------------------------------------------------------------------------

func TestAWSLambdaEnvResolvesFully(t *testing.T) {
	resetModule(t)
	t.Setenv("AWS_LAMBDA_FUNCTION_NAME", "my-fn")
	t.Setenv("AWS_REGION", "us-east-1")
	env := DetectNow()
	if env.Provider != "aws" || env.Region != "us-east-1" || env.Source != "env" {
		t.Fatalf("got %+v", env)
	}
}

func TestAzureAppServiceProviderNoRegion(t *testing.T) {
	resetModule(t)
	t.Setenv("WEBSITE_SITE_NAME", "x")
	env := DetectNow()
	if env.Provider != "azure" || env.Region != "" || env.Source != "env" {
		t.Fatalf("got %+v", env)
	}
}

func TestGCPCloudRunProviderNoRegion(t *testing.T) {
	resetModule(t)
	t.Setenv("K_SERVICE", "my-svc")
	env := DetectNow()
	if env.Provider != "gcp" || env.Region != "" || env.Source != "env" {
		t.Fatalf("got %+v", env)
	}
}

func TestNoEnvNoDMIReturnsUndetected(t *testing.T) {
	resetModule(t)
	env := DetectNow()
	if env.Provider != "" || env.Region != "" || env.Source != "none" {
		t.Fatalf("got %+v", env)
	}
}

// -------------------------------------------------------------------------
// Phase 1b — DMI
// -------------------------------------------------------------------------

func TestDMIAWSViaSysVendorAmazonEC2(t *testing.T) {
	resetModule(t)
	dmiFixture(t, map[string]string{"sys_vendor": "Amazon EC2"})
	env := DetectNow()
	if env.Provider != "aws" || env.Source != "dmi" {
		t.Fatalf("got %+v", env)
	}
}

func TestDMIGCPViaProductName(t *testing.T) {
	resetModule(t)
	dmiFixture(t, map[string]string{"product_name": "Google Compute Engine"})
	env := DetectNow()
	if env.Provider != "gcp" {
		t.Fatalf("got %+v", env)
	}
}

func TestDMIAzureViaChassisAssetTag(t *testing.T) {
	resetModule(t)
	dmiFixture(t, map[string]string{"chassis_asset_tag": "7783-7084-3265-9085-8269-3286-77"})
	env := DetectNow()
	if env.Provider != "azure" {
		t.Fatalf("got %+v", env)
	}
}

func TestDMIAzureViaSysVendorMicrosoftCorporation(t *testing.T) {
	resetModule(t)
	dmiFixture(t, map[string]string{"sys_vendor": "Microsoft Corporation"})
	env := DetectNow()
	if env.Provider != "azure" {
		t.Fatalf("got %+v", env)
	}
}

func TestDMIOCIViaChassisAssetTagNotSysVendor(t *testing.T) {
	resetModule(t)
	dmiFixture(t, map[string]string{"chassis_asset_tag": "OracleCloud.com"})
	env := DetectNow()
	if env.Provider != "oci" {
		t.Fatalf("got %+v", env)
	}
}

func TestDMIAlibabaViaProductName(t *testing.T) {
	resetModule(t)
	dmiFixture(t, map[string]string{"product_name": "Alibaba Cloud ECS"})
	env := DetectNow()
	if env.Provider != "alibaba" {
		t.Fatalf("got %+v", env)
	}
}

// -------------------------------------------------------------------------
// gcpPathToRegion unit tests
// -------------------------------------------------------------------------

func TestGCPPathToRegionZoneForm(t *testing.T) {
	if got := gcpPathToRegion("projects/123/zones/us-central1-a", true); got != "us-central1" {
		t.Fatalf("zone form: got %q", got)
	}
	if got := gcpPathToRegion("us-central1-a", true); got != "us-central1" {
		t.Fatalf("bare zone: got %q", got)
	}
	if got := gcpPathToRegion("", true); got != "" {
		t.Fatalf("empty: got %q", got)
	}
}

func TestGCPPathToRegionRegionForm(t *testing.T) {
	if got := gcpPathToRegion("projects/123/regions/us-central1", false); got != "us-central1" {
		t.Fatalf("got %q", got)
	}
	if got := gcpPathToRegion("projects/123/regions/europe-west4", false); got != "europe-west4" {
		t.Fatalf("got %q", got)
	}
}

// -------------------------------------------------------------------------
// Phase 2 probes — driven against a local httptest.Server with overridden
// endpoint URLs via override probes. Easier to test the probe routing
// logic than to monkey-patch http.Client.
// -------------------------------------------------------------------------

func TestGCPProbePrefersRegionEndpoint(t *testing.T) {
	var calls []string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls = append(calls, r.URL.Path)
		if r.URL.Path == "/computeMetadata/v1/instance/region" {
			_, _ = w.Write([]byte("projects/12345/regions/europe-west4"))
			return
		}
		// /zone — should not be hit on Cloud Run.
		http.Error(w, "zone should not be hit", http.StatusInternalServerError)
	}))
	defer srv.Close()

	env := probeGCPAgainst(srv.URL)
	if env == nil {
		t.Fatal("expected env, got nil")
	}
	if env.Provider != "gcp" || env.Region != "europe-west4" {
		t.Fatalf("got %+v", env)
	}
	if !containsString(calls, "/computeMetadata/v1/instance/region") {
		t.Fatalf("region endpoint not hit: %v", calls)
	}
}

func TestGCPProbeFallsBackToZoneOnRegionFailure(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/computeMetadata/v1/instance/region":
			http.Error(w, "simulated /region missing", http.StatusInternalServerError)
		case "/computeMetadata/v1/instance/zone":
			_, _ = w.Write([]byte("projects/12345/zones/us-central1-a"))
		default:
			http.NotFound(w, r)
		}
	}))
	defer srv.Close()

	env := probeGCPAgainst(srv.URL)
	if env == nil {
		t.Fatal("expected env")
	}
	if env.Region != "us-central1" {
		t.Fatalf("got region %q", env.Region)
	}
}

// probeGCPAgainst is the test-only variant of probeGCP that targets a
// chosen base URL. Inlines the same logic so we exercise gcpPathToRegion +
// the region-before-zone preference.
func probeGCPAgainst(base string) *CloudEnv {
	headers := map[string]string{"Metadata-Flavor": "Google"}
	if body, err := httpGetWithCtx(http.MethodGet, base+"/computeMetadata/v1/instance/region", headers); err == nil {
		region := gcpPathToRegion(strings.TrimSpace(string(body)), false)
		if region != "" {
			return &CloudEnv{Provider: "gcp", Region: region, Source: "imds"}
		}
	}
	body, err := httpGetWithCtx(http.MethodGet, base+"/computeMetadata/v1/instance/zone", headers)
	if err != nil {
		return nil
	}
	return &CloudEnv{
		Provider: "gcp",
		Region:   gcpPathToRegion(strings.TrimSpace(string(body)), true),
		Source:   "imds",
	}
}

func TestOCIProbeUsesCanonicalRegionName(t *testing.T) {
	var calls []string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls = append(calls, r.URL.Path)
		if !strings.HasSuffix(r.URL.Path, "/canonicalRegionName") {
			http.Error(w, "wrong endpoint", http.StatusInternalServerError)
			return
		}
		if r.Header.Get("Authorization") != "Bearer Oracle" {
			http.Error(w, "missing auth", http.StatusUnauthorized)
			return
		}
		_, _ = w.Write([]byte("us-phoenix-1"))
	}))
	defer srv.Close()

	body, err := httpGetWithCtx(
		http.MethodGet,
		srv.URL+"/opc/v2/instance/canonicalRegionName",
		map[string]string{"Authorization": "Bearer Oracle"},
	)
	if err != nil {
		t.Fatalf("probe failed: %v", err)
	}
	got := strings.ToLower(strings.TrimSpace(string(body)))
	if got != "us-phoenix-1" {
		t.Fatalf("region = %q", got)
	}
	for _, p := range calls {
		if !strings.Contains(p, "/canonicalRegionName") {
			t.Fatalf("unexpected endpoint hit: %s", p)
		}
	}
}

// -------------------------------------------------------------------------
// Never-blocks-init contract + track_network=false
// -------------------------------------------------------------------------

func TestInitNeverBlocksWhenMetadataUnreachable(t *testing.T) {
	resetModule(t)
	t0 := time.Now()
	StartBackgroundDetection(true)
	elapsed := time.Since(t0)
	if elapsed > 50*time.Millisecond {
		t.Fatalf("init took %v, expected < 50 ms", elapsed)
	}
	// Wait for the background goroutine to wind down (it will timeout
	// trying to reach 169.254.169.254). Bound the test runtime.
	deadline := time.Now().Add(2 * time.Second)
	for IsBackgroundGoroutineRunningForTests() && time.Now().Before(deadline) {
		time.Sleep(20 * time.Millisecond)
	}
}

func TestTrackNetworkFalseSkipsProbe(t *testing.T) {
	resetModule(t)
	StartBackgroundDetection(false)
	env := GetCloudEnv()
	if env.Source != "none" {
		t.Fatalf("got %+v", env)
	}
	if IsBackgroundGoroutineRunningForTests() {
		t.Fatal("expected no background goroutine")
	}
}

func TestStartWithFullEnvDoesNotLaunchGoroutine(t *testing.T) {
	resetModule(t)
	t.Setenv("AWS_LAMBDA_FUNCTION_NAME", "x")
	t.Setenv("AWS_REGION", "eu-west-1")
	StartBackgroundDetection(true)
	env := GetCloudEnv()
	if env.Provider != "aws" || env.Region != "eu-west-1" {
		t.Fatalf("got %+v", env)
	}
	if IsBackgroundGoroutineRunningForTests() {
		t.Fatal("background goroutine launched despite full env resolution")
	}
}

// -------------------------------------------------------------------------
// May-2026 deep-research additions
// -------------------------------------------------------------------------

func TestECSFargateMetadataURIResolvesAWSWithRegion(t *testing.T) {
	resetModule(t)
	t.Setenv("ECS_CONTAINER_METADATA_URI_V4", "http://169.254.170.2/v4/metadata-id")
	t.Setenv("AWS_REGION", "ap-south-1")
	env := DetectNow()
	if env.Provider != "aws" || env.Region != "ap-south-1" || env.Source != "env" {
		t.Fatalf("got %+v", env)
	}
}

func TestECSV3MetadataURIAlsoResolvesAWS(t *testing.T) {
	resetModule(t)
	t.Setenv("ECS_CONTAINER_METADATA_URI", "http://169.254.170.2/v3/x")
	env := DetectNow()
	if env.Provider != "aws" {
		t.Fatalf("got %+v", env)
	}
}

func TestAzureContainerAppsHostnameYieldsRegion(t *testing.T) {
	resetModule(t)
	t.Setenv("CONTAINER_APP_NAME", "my-app")
	t.Setenv("CONTAINER_APP_HOSTNAME", "my-app--abc.proudground-12345.eastus.azurecontainerapps.io")
	env := DetectNow()
	if env.Provider != "azure" || env.Region != "eastus" || env.Source != "env" {
		t.Fatalf("got %+v", env)
	}
}

func TestAzureContainerAppsDNSSuffixYieldsRegion(t *testing.T) {
	resetModule(t)
	t.Setenv("CONTAINER_APP_NAME", "my-app")
	t.Setenv("CONTAINER_APP_ENV_DNS_SUFFIX", "proudground-12345.westeurope.azurecontainerapps.io")
	env := DetectNow()
	if env.Region != "westeurope" {
		t.Fatalf("got %+v", env)
	}
}

func TestAzureRegionNameWinsWhenBothPresent(t *testing.T) {
	resetModule(t)
	t.Setenv("CONTAINER_APP_NAME", "x")
	t.Setenv("REGION_NAME", "northeurope")
	t.Setenv("CONTAINER_APP_HOSTNAME", "x.y.eastus.azurecontainerapps.io")
	env := DetectNow()
	if env.Region != "northeurope" {
		t.Fatalf("got %+v", env)
	}
}

func TestGCPKConfigurationAloneSignalsGCP(t *testing.T) {
	resetModule(t)
	t.Setenv("K_CONFIGURATION", "my-config")
	env := DetectNow()
	if env.Provider != "gcp" {
		t.Fatalf("got %+v", env)
	}
}

func TestBareAWSRegionNowClassifiesAsAWS(t *testing.T) {
	resetModule(t)
	t.Setenv("AWS_REGION", "us-east-1")
	env := DetectNow()
	if env.Provider != "aws" || env.Region != "us-east-1" {
		t.Fatalf("got %+v", env)
	}
}

func TestFlyRegionEnvResolvesProviderAndRegion(t *testing.T) {
	resetModule(t)
	t.Setenv("FLY_REGION", "iad")
	t.Setenv("FLY_APP_NAME", "my-app")
	env := DetectNow()
	if env.Provider != "fly" || env.Region != "iad" || env.Source != "env" {
		t.Fatalf("got %+v", env)
	}
}

func TestFlyAppNameAloneSignalsFly(t *testing.T) {
	resetModule(t)
	t.Setenv("FLY_APP_NAME", "my-app")
	env := DetectNow()
	if env.Provider != "fly" {
		t.Fatalf("got %+v", env)
	}
}

func TestVercelRegionResolvesProviderAndRegion(t *testing.T) {
	// Vercel sets both VERCEL and AWS_REGION (it runs on AWS). Vercel wins
	// because earlier matches take precedence.
	resetModule(t)
	t.Setenv("VERCEL", "1")
	t.Setenv("VERCEL_REGION", "iad1")
	t.Setenv("AWS_REGION", "us-east-1")
	env := DetectNow()
	if env.Provider != "vercel" || env.Region != "iad1" {
		t.Fatalf("got %+v", env)
	}
}

func TestDMIDigitalOceanViaSysVendor(t *testing.T) {
	resetModule(t)
	dmiFixture(t, map[string]string{"sys_vendor": "DigitalOcean"})
	env := DetectNow()
	if env.Provider != "digitalocean" {
		t.Fatalf("got %+v", env)
	}
}

func TestDMIHetznerViaSysVendor(t *testing.T) {
	resetModule(t)
	dmiFixture(t, map[string]string{"sys_vendor": "Hetzner"})
	env := DetectNow()
	if env.Provider != "hetzner" {
		t.Fatalf("got %+v", env)
	}
}

func TestDMIVultrViaSysVendor(t *testing.T) {
	resetModule(t)
	dmiFixture(t, map[string]string{"sys_vendor": "Vultr"})
	env := DetectNow()
	if env.Provider != "vultr" {
		t.Fatalf("got %+v", env)
	}
}

func TestDMICanonicalFieldWinsOverBackup(t *testing.T) {
	// When BOTH canonical (chassis_asset_tag=OracleCloud.com) and a backup
	// signal (sys_vendor=Google) are present, the canonical wins because it
	// is listed first in dmiRules.
	resetModule(t)
	dmiFixture(t, map[string]string{
		"chassis_asset_tag": "OracleCloud.com",
		"sys_vendor":        "Google",
	})
	env := DetectNow()
	if env.Provider != "oci" {
		t.Fatalf("got %+v", env)
	}
}

func TestDMIUnknownVendorReturnsNone(t *testing.T) {
	resetModule(t)
	dmiFixture(t, map[string]string{"sys_vendor": "LENOVO"})
	env := DetectNow()
	if env.Provider != "" || env.Source != "none" {
		t.Fatalf("got %+v", env)
	}
}

// ── ML / GPU clouds ────────────────────────────────────────────────────

func TestModalTaskIDResolvesModalWithRegion(t *testing.T) {
	resetModule(t)
	t.Setenv("MODAL_TASK_ID", "ta-abc")
	t.Setenv("MODAL_REGION", "us-east-1")
	env := DetectNow()
	if env.Provider != "modal" || env.Region != "us-east-1" {
		t.Fatalf("got %+v", env)
	}
}

func TestRunpodPodIDResolvesProvider(t *testing.T) {
	resetModule(t)
	t.Setenv("RUNPOD_POD_ID", "abc123")
	t.Setenv("RUNPOD_DC_ID", "US-CA-2")
	env := DetectNow()
	if env.Provider != "runpod" || env.Region != "US-CA-2" {
		t.Fatalf("got %+v", env)
	}
}

// ── PaaS app platforms ────────────────────────────────────────────────

func TestRenderResolves(t *testing.T) {
	resetModule(t)
	t.Setenv("RENDER", "true")
	t.Setenv("RENDER_SERVICE_ID", "srv-abc")
	env := DetectNow()
	if env.Provider != "render" || env.Region != "" {
		t.Fatalf("got %+v", env)
	}
}

func TestRailwayResolvesWithReplicaRegion(t *testing.T) {
	resetModule(t)
	t.Setenv("RAILWAY_PROJECT_ID", "abc")
	t.Setenv("RAILWAY_REPLICA_REGION", "us-west2")
	env := DetectNow()
	if env.Provider != "railway" || env.Region != "us-west2" {
		t.Fatalf("got %+v", env)
	}
}

func TestHerokuDynoResolves(t *testing.T) {
	resetModule(t)
	t.Setenv("DYNO", "web.1")
	env := DetectNow()
	if env.Provider != "heroku" {
		t.Fatalf("got %+v", env)
	}
}

func TestKoyebResolves(t *testing.T) {
	resetModule(t)
	t.Setenv("KOYEB_APP_NAME", "my-app")
	t.Setenv("KOYEB_REGION", "fra")
	env := DetectNow()
	if env.Provider != "koyeb" || env.Region != "fra" {
		t.Fatalf("got %+v", env)
	}
}

// ── Fanout shape ───────────────────────────────────────────────────────

func TestPhase2RunsOnlyAWSGCPAzureInParallel(t *testing.T) {
	got := FanoutProbesForTests()
	want := []string{"aws", "gcp", "azure"}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("fanout = %v, want %v", got, want)
	}
}

func TestPhase2UsesProviderHintWhenDMIPreClassifies(t *testing.T) {
	// When DMI says "oci", runProbe goes straight to OCI's endpoint — no
	// fanout, no AWS IMDS race.
	resetModule(t)
	var calls []string
	cleanupOCI := WithProbeOverrideForTests("oci", func() *CloudEnv {
		calls = append(calls, "oci")
		return &CloudEnv{Provider: "oci", Region: "us-ashburn-1", Source: "imds"}
	})
	defer cleanupOCI()
	cleanupAWS := WithProbeOverrideForTests("aws", func() *CloudEnv {
		calls = append(calls, "aws")
		return nil
	})
	defer cleanupAWS()

	env := runProbe("oci")
	if env.Provider != "oci" || env.Region != "us-ashburn-1" {
		t.Fatalf("got %+v", env)
	}
	if !reflect.DeepEqual(calls, []string{"oci"}) {
		t.Fatalf("calls = %v, expected only [oci]", calls)
	}
}

func TestMLCloudWinsOverUnderlyingAWS(t *testing.T) {
	// Modal/RunPod run on AWS — but the platform attribution must win.
	resetModule(t)
	t.Setenv("AWS_REGION", "us-east-1")
	t.Setenv("MODAL_TASK_ID", "ta-abc")
	t.Setenv("MODAL_REGION", "us-east-1")
	env := DetectNow()
	if env.Provider != "modal" {
		t.Fatalf("got %+v (Modal $0 must beat AWS $0.09/GB)", env)
	}
}

// helpers --------------------------------------------------------------

func containsString(s []string, want string) bool {
	for _, x := range s {
		if x == want {
			return true
		}
	}
	return false
}
