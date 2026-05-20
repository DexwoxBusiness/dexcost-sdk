// Compute catalog integrity — structure, Decimal parsing, freshness,
// dispatch coverage. Mirrors python/tests/test_compute_catalog_integrity.py.

package pricing

import (
	_ "embed"
	"encoding/json"
	"strings"
	"testing"
	"time"

	"github.com/shopspring/decimal"
)

//go:embed data/compute_prices.json
var embeddedComputeCatalogForTests []byte

func loadComputeCatalog(t *testing.T) map[string]any {
	t.Helper()
	var data map[string]any
	if err := json.Unmarshal(embeddedComputeCatalogForTests, &data); err != nil {
		t.Fatalf("compute_prices.json parse: %v", err)
	}
	return data
}

func TestComputeCatalogParsesAsJSON(t *testing.T) {
	data := loadComputeCatalog(t)
	if _, ok := data["_meta"]; !ok {
		t.Fatal("_meta missing")
	}
}

func TestComputeMetaHasRequiredDefaultKeys(t *testing.T) {
	data := loadComputeCatalog(t)
	meta, ok := data["_meta"].(map[string]any)
	if !ok {
		t.Fatal("_meta not a map")
	}
	required := []string{
		"version", "last_updated", "currency",
		"default_lambda_request_usd", "default_lambda_gb_second_usd",
		"default_fargate_vcpu_second_usd", "default_fargate_gib_second_usd",
		"default_cloud_run_request_usd", "default_cloud_run_vcpu_second_usd",
		"default_cloud_run_gib_second_usd",
		"default_azure_functions_execution_usd",
		"default_azure_functions_gb_second_usd",
		"default_vercel_cpu_hour_usd", "default_vercel_memory_gb_hour_usd",
		"default_ec2_vcpu_hour_usd", "default_k8s_pod_vcpu_hour_usd",
		"description", "notes",
	}
	for _, k := range required {
		v, present := meta[k]
		if !present {
			t.Errorf("_meta missing %s", k)
			continue
		}
		if strings.HasPrefix(k, "default_") && strings.HasSuffix(k, "_usd") {
			s, ok := v.(string)
			if !ok {
				t.Errorf("_meta.%s not a string: %T", k, v)
				continue
			}
			if _, err := decimal.NewFromString(s); err != nil {
				t.Errorf("_meta.%s = %q not Decimal-parseable: %v", k, s, err)
			}
		}
	}
	if meta["currency"] != "USD" {
		t.Errorf("currency = %v, want USD", meta["currency"])
	}
}

func TestComputeEveryProviderHasLastVerified(t *testing.T) {
	data := loadComputeCatalog(t)
	today := time.Now().UTC()
	softLimit := 180 * 24 * time.Hour
	for provider, raw := range data {
		if provider == "_meta" {
			continue
		}
		block, ok := raw.(map[string]any)
		if !ok {
			t.Errorf("provider %s not a map", provider)
			continue
		}
		v, ok := block["_last_verified"].(string)
		if !ok {
			t.Errorf("provider %s missing _last_verified", provider)
			continue
		}
		verified, err := time.Parse("2006-01-02", v)
		if err != nil {
			t.Errorf("provider %s _last_verified %q not ISO date: %v", provider, v, err)
			continue
		}
		if age := today.Sub(verified); age > softLimit {
			// Soft warn — t.Logf (NOT t.Errorf) per plan.
			t.Logf("compute_prices.json: %s _last_verified is %d days old (soft limit 180)",
				provider, int(age.Hours()/24))
		}
	}
}

func TestComputeAllProvidersAndRuntimesPresent(t *testing.T) {
	data := loadComputeCatalog(t)
	for _, p := range []string{"aws", "gcp", "azure", "vercel"} {
		if _, ok := data[p]; !ok {
			t.Errorf("provider %s missing", p)
		}
	}
	aws, _ := data["aws"].(map[string]any)
	for _, r := range []string{"lambda", "fargate", "ec2"} {
		if _, ok := aws[r]; !ok {
			t.Errorf("aws.%s missing", r)
		}
	}
	gcp, _ := data["gcp"].(map[string]any)
	for _, r := range []string{"cloud_run", "cloud_functions", "gce"} {
		if _, ok := gcp[r]; !ok {
			t.Errorf("gcp.%s missing", r)
		}
	}
	azure, _ := data["azure"].(map[string]any)
	for _, r := range []string{"functions_consumption", "vm"} {
		if _, ok := azure[r]; !ok {
			t.Errorf("azure.%s missing", r)
		}
	}
	vercel, _ := data["vercel"].(map[string]any)
	if _, ok := vercel["fluid"]; !ok {
		t.Error("vercel.fluid missing")
	}
}

func TestLambdaHasBothArchitectures(t *testing.T) {
	data := loadComputeCatalog(t)
	def := getPath(t, data, "aws", "lambda", "default").(map[string]any)
	for _, arch := range []string{"x86_64", "arm64"} {
		ad, ok := def[arch].(map[string]any)
		if !ok {
			t.Errorf("aws.lambda.default missing %s", arch)
			continue
		}
		mustDecimal(t, ad, "request_usd")
		mustDecimal(t, ad, "gb_second_usd")
	}
}

func TestFargateHasBothArchitectures(t *testing.T) {
	data := loadComputeCatalog(t)
	def := getPath(t, data, "aws", "fargate", "default").(map[string]any)
	for _, arch := range []string{"x86_64", "arm64"} {
		ad, ok := def[arch].(map[string]any)
		if !ok {
			t.Errorf("aws.fargate.default missing %s", arch)
			continue
		}
		mustDecimal(t, ad, "vcpu_second_usd")
		mustDecimal(t, ad, "gib_second_usd")
	}
}

func TestARMCheaperThanX86OnLambda(t *testing.T) {
	data := loadComputeCatalog(t)
	regions := getPath(t, data, "aws", "lambda", "regions").(map[string]any)
	var any1 map[string]any
	for _, v := range regions {
		any1 = v.(map[string]any)
		break
	}
	arm := decimal.RequireFromString(any1["arm64"].(map[string]any)["gb_second_usd"].(string))
	x86 := decimal.RequireFromString(any1["x86_64"].(map[string]any)["gb_second_usd"].(string))
	if !arm.LessThan(x86) {
		t.Fatalf("arm64 (%s) must be cheaper than x86_64 (%s) on Lambda", arm, x86)
	}
}

func TestARMCheaperThanX86OnFargate(t *testing.T) {
	data := loadComputeCatalog(t)
	regions := getPath(t, data, "aws", "fargate", "regions").(map[string]any)
	var any1 map[string]any
	for _, v := range regions {
		any1 = v.(map[string]any)
		break
	}
	arm := decimal.RequireFromString(any1["arm64"].(map[string]any)["vcpu_second_usd"].(string))
	x86 := decimal.RequireFromString(any1["x86_64"].(map[string]any)["vcpu_second_usd"].(string))
	if !arm.LessThan(x86) {
		t.Fatalf("arm64 (%s) must be cheaper than x86_64 (%s) on Fargate", arm, x86)
	}
}

func TestTopInstanceTypesPresentForEC2USEast1(t *testing.T) {
	data := loadComputeCatalog(t)
	regions := getPath(t, data, "aws", "ec2", "regions").(map[string]any)
	use1, ok := regions["us-east-1"].(map[string]any)
	if !ok {
		t.Fatal("aws.ec2.regions.us-east-1 missing")
	}
	instances, ok := use1["instance_types"].(map[string]any)
	if !ok {
		t.Fatal("us-east-1.instance_types missing")
	}
	for _, want := range []string{"c7g.xlarge", "m7i.large", "t3.medium"} {
		v, ok := instances[want]
		if !ok {
			t.Errorf("missing EC2 SKU: %s", want)
			continue
		}
		row, _ := v.(map[string]any)
		mustDecimal(t, row, "hourly_usd")
		mustDecimal(t, row, "vcpu_count")
	}
}

func TestTopInstanceTypesPresentForGCEUSCentral1(t *testing.T) {
	data := loadComputeCatalog(t)
	instances := getPath(t, data, "gcp", "gce", "regions", "us-central1", "instance_types").(map[string]any)
	for _, want := range []string{"n2-standard-2", "e2-standard-4"} {
		if _, ok := instances[want]; !ok {
			t.Errorf("missing GCE SKU: %s", want)
		}
	}
}

func TestTopInstanceTypesPresentForAzureVMEastus(t *testing.T) {
	data := loadComputeCatalog(t)
	instances := getPath(t, data, "azure", "vm", "regions", "eastus", "instance_types").(map[string]any)
	for _, want := range []string{"Standard_D2s_v3", "Standard_B2ms"} {
		if _, ok := instances[want]; !ok {
			t.Errorf("missing Azure VM SKU: %s", want)
		}
	}
}

func TestEveryComputeRateIsDecimalParseable(t *testing.T) {
	data := loadComputeCatalog(t)
	var walk func(node any, path string)
	walk = func(node any, path string) {
		switch v := node.(type) {
		case map[string]any:
			for k, child := range v {
				walk(child, path+"."+k)
			}
		case string:
			if strings.HasSuffix(path, "_usd") || strings.HasSuffix(path, "vcpu_count") {
				if _, err := decimal.NewFromString(v); err != nil {
					t.Errorf("%s not Decimal-parseable: %q (%v)", path, v, err)
				}
			}
		}
	}
	walk(data, "")
}

func TestEveryDispatchBillingModelHasARatePath(t *testing.T) {
	data := loadComputeCatalog(t)
	meta := data["_meta"].(map[string]any)
	required := []string{
		"default_lambda_request_usd",
		"default_fargate_vcpu_second_usd",
		"default_cloud_run_request_usd",
		"default_azure_functions_execution_usd",
		"default_vercel_cpu_hour_usd",
		"default_ec2_vcpu_hour_usd",
		"default_k8s_pod_vcpu_hour_usd",
	}
	for _, k := range required {
		if _, ok := meta[k]; !ok {
			t.Errorf("_meta.%s missing — billing model unreachable", k)
		}
	}
}

// getPath walks a nested map by string keys, failing the test on miss.
func getPath(t *testing.T, root any, keys ...string) any {
	t.Helper()
	cur := root
	for _, k := range keys {
		m, ok := cur.(map[string]any)
		if !ok {
			t.Fatalf("path %v: not a map at %q", keys, k)
		}
		cur, ok = m[k]
		if !ok {
			t.Fatalf("path %v: missing key %q", keys, k)
		}
	}
	return cur
}

func mustDecimal(t *testing.T, m map[string]any, key string) {
	t.Helper()
	s, ok := m[key].(string)
	if !ok {
		t.Errorf("%s not a string", key)
		return
	}
	if _, err := decimal.NewFromString(s); err != nil {
		t.Errorf("%s = %q not Decimal-parseable: %v", key, s, err)
	}
}
