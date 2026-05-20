// Compute pricing engine — dispatches on details.billing_model and applies
// the per-billing-model math from spec §6.
//
// The per-runtime memory-unit conversion table (Decision #7) is pinned at
// the catalog-lookup boundary in §6.2 of the spec; the implementation
// enforces it via two Decimal divisor constants (decimal GB vs binary GiB)
// selected per billing model. Confusing them silently over-attributes
// Fargate memory cost by ~4.86%.
//
// Fail-silent contract (convention §9): every code path returns a usable
// ComputeCost — the five-tier degradation ladder from convention §7 applies
// (per-region exact → per-runtime default → universal _meta default →
// hardcoded constants → cost=0 with warning).
//
// Mirrors python/src/dexcost/compute_pricing.py.

package pricing

import (
	_ "embed"
	"encoding/json"
	"fmt"
	"log"
	"os"
	"strings"
	"sync"
	"sync/atomic"

	"github.com/shopspring/decimal"

	"github.com/DexwoxBusiness/dexcost-go/cloud"
)

//go:embed data/compute_prices.json
var embeddedComputeCatalog []byte

// ─── Conversion constants (Decision #7 pinned table) ────────────────────

var (
	gbDecimal = decimal.NewFromInt(1_000_000_000)   // 10^9 — Lambda / Azure Functions / Vercel
	gibBinary = decimal.NewFromInt(1024 * 1024 * 1024) // 2^30 — Fargate / Cloud Run
	hourS     = decimal.NewFromInt(3600)
	msPerS    = decimal.NewFromInt(1000)
)

// ─── Tier-4 hardcoded constants (must mirror _meta defaults) ────────────

var hardcoded = map[string]map[string]decimal.Decimal{
	"lambda": {
		"request_usd":   decimal.RequireFromString("0.0000002"),
		"gb_second_usd": decimal.RequireFromString("0.0000166667"),
	},
	"fargate": {
		"vcpu_second_usd": decimal.RequireFromString("0.0000112444"),
		"gib_second_usd":  decimal.RequireFromString("0.0000012347"),
	},
	"cloud_run_request": {
		"request_usd":     decimal.RequireFromString("0.0000004"),
		"vcpu_second_usd": decimal.RequireFromString("0.000024"),
		"gib_second_usd":  decimal.RequireFromString("0.0000025"),
	},
	"cloud_run_instance": {
		"vcpu_second_usd": decimal.RequireFromString("0.000024"),
		"gib_second_usd":  decimal.RequireFromString("0.0000025"),
	},
	"cloud_functions": {
		"request_usd":     decimal.RequireFromString("0.0000004"),
		"vcpu_second_usd": decimal.RequireFromString("0.000024"),
		"gib_second_usd":  decimal.RequireFromString("0.0000025"),
	},
	"azure_functions": {
		"execution_usd": decimal.RequireFromString("0.0000002"),
		"gb_second_usd": decimal.RequireFromString("0.000016"),
	},
	"vercel_fluid": {
		"active_cpu_hour_usd": decimal.RequireFromString("0.128"),
		"memory_gb_hour_usd":  decimal.RequireFromString("0.0106"),
		"invocation_usd":      decimal.RequireFromString("0.000000600"),
	},
	"ec2":      {"vcpu_hour_usd": decimal.RequireFromString("0.0464")},
	"gce":      {"vcpu_hour_usd": decimal.RequireFromString("0.0475")},
	"azure_vm": {"vcpu_hour_usd": decimal.RequireFromString("0.046")},
	"k8s_pod":  {"vcpu_hour_usd": decimal.RequireFromString("0.0464")},
}

// ─── Warn-once state (convention §11) ───────────────────────────────────

var (
	computeWarnMu    sync.Mutex
	computeWarned    = map[string]struct{}{}
	computeWarnCount int32 // test-visible counter
	computeWarnLogf  = func(format string, args ...any) {
		log.Printf("WARN dexcost.compute: "+format, args...)
	}
)

// ResetComputeWarningStateForTests clears the warned-modes set + counter.
func ResetComputeWarningStateForTests() {
	computeWarnMu.Lock()
	defer computeWarnMu.Unlock()
	computeWarned = map[string]struct{}{}
	atomic.StoreInt32(&computeWarnCount, 0)
}

// captureComputeWarnLogs swaps the warn-logger for a counter for the
// duration of the test.
func captureComputeWarnLogs(t interface{ Cleanup(func()) }) *int32 {
	old := computeWarnLogf
	computeWarnLogf = func(format string, args ...any) {
		atomic.AddInt32(&computeWarnCount, 1)
	}
	t.Cleanup(func() { computeWarnLogf = old })
	return &computeWarnCount
}

func warnOnce(mode, format string, args ...any) {
	computeWarnMu.Lock()
	if _, seen := computeWarned[mode]; seen {
		computeWarnMu.Unlock()
		return
	}
	computeWarned[mode] = struct{}{}
	computeWarnMu.Unlock()
	computeWarnLogf(format, args...)
}

// ─── Types ──────────────────────────────────────────────────────────────

// ComputeCost is the per-event resolved cost.
type ComputeCost struct {
	CostUSD        decimal.Decimal
	PricingSource  string
	CostConfidence string // computed | estimated | exact | unknown
}

// ComputePricingEngine resolves compute cost per compute_cost event details.
type ComputePricingEngine struct {
	catalog        map[string]any
	catalogVersion string
}

// NewComputePricingEngine loads the embedded catalog.
func NewComputePricingEngine() *ComputePricingEngine {
	eng := &ComputePricingEngine{catalogVersion: "unknown"}
	eng.parseCatalog(embeddedComputeCatalog)
	return eng
}

// NewComputePricingEngineFromPath is the test-only variant that reads from
// a filesystem path. Missing files trigger the Tier-4 hardcoded fallback;
// the engine is still returned (never nil) so callers can rely on it.
func NewComputePricingEngineFromPath(path string) (*ComputePricingEngine, error) {
	eng := &ComputePricingEngine{catalogVersion: "unknown"}
	raw, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			warnOnce("catalog_missing",
				"compute catalog file not found; falling back to hardcoded per-billing-model defaults")
		} else {
			warnOnce("catalog_unreadable",
				"compute catalog unreadable (%v); falling back to hardcoded per-billing-model defaults", err)
		}
		return eng, nil
	}
	eng.parseCatalog(raw)
	return eng, nil
}

func (e *ComputePricingEngine) parseCatalog(raw []byte) {
	var data map[string]any
	if err := json.Unmarshal(raw, &data); err != nil {
		warnOnce("catalog_malformed",
			"compute catalog malformed JSON (%v); falling back to hardcoded per-billing-model defaults", err)
		return
	}
	e.catalog = data
	if meta, ok := data["_meta"].(map[string]any); ok {
		if v, ok := meta["version"].(string); ok {
			e.catalogVersion = v
		}
	}
}

// CatalogVersion exposes the loaded catalog's _meta.version.
func (e *ComputePricingEngine) CatalogVersion() string { return e.catalogVersion }

// ─── Public entry point — Tier 5 wrapper ────────────────────────────────

// ResolveComputeCost computes cost for one compute_cost event. Returns a
// usable ComputeCost in every case — Tier 5 wraps the dispatch in a
// recover() so a pricing bug cannot break task finalize.
func (e *ComputePricingEngine) ResolveComputeCost(
	details map[string]any,
	cloudEnv cloud.CloudEnv,
	overrides map[string]string,
	windowS decimal.Decimal,
) (result ComputeCost) {
	billingModel := getString(details, "billing_model")
	if billingModel == "" {
		billingModel = "unknown"
	}
	defer func() {
		if r := recover(); r != nil {
			warnOnce(
				"compute_failure:"+billingModel,
				"compute pricing failed for billing_model=%s: %v; emitting cost_usd=0",
				billingModel, r,
			)
			result = ComputeCost{
				CostUSD:        decimal.Zero,
				PricingSource:  "compute_catalog:error:" + billingModel,
				CostConfidence: "unknown",
			}
		}
	}()
	return e.dispatch(billingModel, details, cloudEnv, overrides, windowS)
}

// ─── Dispatch ───────────────────────────────────────────────────────────

func (e *ComputePricingEngine) dispatch(
	billingModel string,
	details map[string]any,
	cloudEnv cloud.CloudEnv,
	overrides map[string]string,
	windowS decimal.Decimal,
) ComputeCost {
	// Cloud Run override — flip the math BEFORE catalog lookup.
	if billingModel == "cloud_run_request" && overrides["cloud_run"] == "instance" {
		return e.cloudRunInstanceOverride(details, windowS)
	}

	switch billingModel {
	case "lambda":
		return e.lambdaCost(details)
	case "fargate":
		return e.fargateCost(details, windowS)
	case "cloud_run_request":
		return e.cloudRunRequest(details)
	case "cloud_run_instance":
		return e.cloudRunInstanceOverride(details, windowS)
	case "cloud_functions":
		return e.cloudFunctionsCost(details)
	case "azure_functions":
		return e.azureFunctionsCost(details)
	case "vercel_fluid":
		return e.vercelCost(details)
	case "ec2", "gce", "azure_vm":
		return e.iaasShare(billingModel, details, cloudEnv, windowS)
	case "k8s_pod":
		return e.k8sPodLimits(details, windowS)
	}

	warnOnce(
		"unsupported_billing_model:"+billingModel,
		"compute pricing has no math for billing_model=%s; emitting cost_usd=0",
		billingModel,
	)
	return ComputeCost{
		CostUSD:        decimal.Zero,
		PricingSource:  "compute_catalog:unsupported:" + billingModel,
		CostConfidence: "unknown",
	}
}

// ─── Lambda ─────────────────────────────────────────────────────────────

func (e *ComputePricingEngine) lambdaCost(details map[string]any) ComputeCost {
	region := getString(details, "region")
	architecture := getString(details, "architecture")
	if architecture == "" {
		architecture = "x86_64"
	}
	rate, source, confidence := e.resolveLambdaRate(region, architecture)
	durationS := dec(details, "duration_ms").Div(msPerS)
	memoryGB := dec(details, "memory_bytes_limit").Div(gbDecimal)
	gbSeconds := memoryGB.Mul(durationS)
	invocations := dec(details, "invocation_count")
	cost := invocations.Mul(rate["request_usd"]).Add(gbSeconds.Mul(rate["gb_second_usd"]))
	return ComputeCost{CostUSD: cost, PricingSource: source, CostConfidence: confidence}
}

func (e *ComputePricingEngine) resolveLambdaRate(region, arch string) (
	map[string]decimal.Decimal, string, string,
) {
	if block, ok := nestedMap(e.catalog, "aws", "lambda"); ok {
		if regions, ok := nestedMap(block, "regions"); ok {
			if region != "" {
				if rblock, ok := nestedMap(regions, region); ok {
					if archBlock, ok := nestedMap(rblock, arch); ok {
						return parseRateBlock(archBlock, "request_usd", "gb_second_usd"),
							fmt.Sprintf("compute_catalog:aws:lambda:%s:%s", region, arch),
							"computed"
					}
				}
			}
		}
		if defBlock, ok := nestedMap(block, "default"); ok {
			if archBlock, ok := nestedMap(defBlock, arch); ok {
				return parseRateBlock(archBlock, "request_usd", "gb_second_usd"),
					fmt.Sprintf("compute_catalog:aws:lambda:default:%s", arch),
					"estimated"
			}
		}
	}
	// Tier 3 → _meta defaults.
	if meta, ok := nestedMap(e.catalog, "_meta"); ok {
		req, errA := decimalFromMeta(meta, "default_lambda_request_usd")
		gbs, errB := decimalFromMeta(meta, "default_lambda_gb_second_usd")
		if errA == nil && errB == nil {
			return map[string]decimal.Decimal{
				"request_usd":   req,
				"gb_second_usd": gbs,
			}, "compute_catalog:default:lambda", "estimated"
		}
	}
	return hardcoded["lambda"], "compute_catalog:hardcoded:lambda", "estimated"
}

// ─── Fargate ────────────────────────────────────────────────────────────

func (e *ComputePricingEngine) fargateCost(details map[string]any, windowS decimal.Decimal) ComputeCost {
	if !windowS.IsPositive() {
		windowS = dec(details, "duration_ms").Div(msPerS)
	}
	region := getString(details, "region")
	arch := getString(details, "architecture")
	if arch == "" {
		arch = "x86_64"
	}
	rate, source, confidence := e.resolveFargateRate(region, arch)
	memoryGiB := dec(details, "memory_bytes_limit").Div(gibBinary)
	vcpuCount := dec(details, "vcpu_count")
	cost := vcpuCount.Mul(windowS).Mul(rate["vcpu_second_usd"]).
		Add(memoryGiB.Mul(windowS).Mul(rate["gib_second_usd"]))
	return ComputeCost{CostUSD: cost, PricingSource: source, CostConfidence: confidence}
}

func (e *ComputePricingEngine) resolveFargateRate(region, arch string) (
	map[string]decimal.Decimal, string, string,
) {
	if block, ok := nestedMap(e.catalog, "aws", "fargate"); ok {
		if regions, ok := nestedMap(block, "regions"); ok {
			if region != "" {
				if rblock, ok := nestedMap(regions, region); ok {
					if archBlock, ok := nestedMap(rblock, arch); ok {
						return parseRateBlock(archBlock, "vcpu_second_usd", "gib_second_usd"),
							fmt.Sprintf("compute_catalog:aws:fargate:%s:%s", region, arch),
							"computed"
					}
				}
			}
		}
		if defBlock, ok := nestedMap(block, "default"); ok {
			if archBlock, ok := nestedMap(defBlock, arch); ok {
				return parseRateBlock(archBlock, "vcpu_second_usd", "gib_second_usd"),
					fmt.Sprintf("compute_catalog:aws:fargate:default:%s", arch),
					"estimated"
			}
		}
	}
	if meta, ok := nestedMap(e.catalog, "_meta"); ok {
		v, errA := decimalFromMeta(meta, "default_fargate_vcpu_second_usd")
		g, errB := decimalFromMeta(meta, "default_fargate_gib_second_usd")
		if errA == nil && errB == nil {
			return map[string]decimal.Decimal{
				"vcpu_second_usd": v,
				"gib_second_usd":  g,
			}, "compute_catalog:default:fargate", "estimated"
		}
	}
	return hardcoded["fargate"], "compute_catalog:hardcoded:fargate", "estimated"
}

// ─── Cloud Run (request-based default) ─────────────────────────────────

func (e *ComputePricingEngine) cloudRunRequest(details map[string]any) ComputeCost {
	region := getString(details, "region")
	rate, _, _ := e.resolveCloudRunRate(region)
	// Decision #1: Cloud Run defaults to request-based with estimated
	// confidence — the container cannot discover the actual billing mode.
	source := "compute_catalog:cloud_run:request_based_default"
	confidence := "estimated"
	durationS := dec(details, "duration_ms").Div(msPerS)
	memoryGiB := dec(details, "memory_bytes_limit").Div(gibBinary)
	vcpuCount := dec(details, "vcpu_count")
	invocations := dec(details, "invocation_count")
	cost := invocations.Mul(rate["request_usd"]).
		Add(vcpuCount.Mul(durationS).Mul(rate["vcpu_second_usd"])).
		Add(memoryGiB.Mul(durationS).Mul(rate["gib_second_usd"]))
	return ComputeCost{CostUSD: cost, PricingSource: source, CostConfidence: confidence}
}

func (e *ComputePricingEngine) cloudRunInstanceOverride(details map[string]any, windowS decimal.Decimal) ComputeCost {
	if !windowS.IsPositive() {
		windowS = dec(details, "duration_ms").Div(msPerS)
	}
	region := getString(details, "region")
	rate, _, _ := e.resolveCloudRunRate(region)
	memoryGiB := dec(details, "memory_bytes_limit").Div(gibBinary)
	vcpuCount := dec(details, "vcpu_count")
	cost := vcpuCount.Mul(windowS).Mul(rate["vcpu_second_usd"]).
		Add(memoryGiB.Mul(windowS).Mul(rate["gib_second_usd"]))
	return ComputeCost{
		CostUSD:        cost,
		PricingSource:  "compute_catalog:cloud_run:instance_override",
		CostConfidence: "computed",
	}
}

func (e *ComputePricingEngine) resolveCloudRunRate(region string) (
	map[string]decimal.Decimal, string, string,
) {
	if block, ok := nestedMap(e.catalog, "gcp", "cloud_run"); ok {
		if regions, ok := nestedMap(block, "regions"); ok {
			if region != "" {
				if rblock, ok := nestedMap(regions, region); ok {
					return parseRateBlock(rblock, "request_usd", "vcpu_second_usd", "gib_second_usd"),
						fmt.Sprintf("compute_catalog:gcp:cloud_run:%s", region),
						"computed"
				}
			}
		}
		if defBlock, ok := nestedMap(block, "default"); ok {
			return parseRateBlock(defBlock, "request_usd", "vcpu_second_usd", "gib_second_usd"),
				"compute_catalog:gcp:cloud_run:default", "estimated"
		}
	}
	if meta, ok := nestedMap(e.catalog, "_meta"); ok {
		r, errA := decimalFromMeta(meta, "default_cloud_run_request_usd")
		v, errB := decimalFromMeta(meta, "default_cloud_run_vcpu_second_usd")
		g, errC := decimalFromMeta(meta, "default_cloud_run_gib_second_usd")
		if errA == nil && errB == nil && errC == nil {
			return map[string]decimal.Decimal{
				"request_usd":     r,
				"vcpu_second_usd": v,
				"gib_second_usd":  g,
			}, "compute_catalog:default:cloud_run", "estimated"
		}
	}
	return hardcoded["cloud_run_request"], "compute_catalog:hardcoded:cloud_run", "estimated"
}

// ─── Cloud Functions Gen2 ───────────────────────────────────────────────

func (e *ComputePricingEngine) cloudFunctionsCost(details map[string]any) ComputeCost {
	region := getString(details, "region")
	rate, source, confidence := e.resolveCloudRunRate(region)
	source = strings.Replace(source, "cloud_run", "cloud_functions", 1)
	durationS := dec(details, "duration_ms").Div(msPerS)
	memoryGiB := dec(details, "memory_bytes_limit").Div(gibBinary)
	vcpuCount := dec(details, "vcpu_count")
	invocations := dec(details, "invocation_count")
	cost := invocations.Mul(rate["request_usd"]).
		Add(vcpuCount.Mul(durationS).Mul(rate["vcpu_second_usd"])).
		Add(memoryGiB.Mul(durationS).Mul(rate["gib_second_usd"]))
	return ComputeCost{CostUSD: cost, PricingSource: source, CostConfidence: confidence}
}

// ─── Azure Functions Consumption ────────────────────────────────────────

func (e *ComputePricingEngine) azureFunctionsCost(details map[string]any) ComputeCost {
	region := getString(details, "region")
	rate, source, confidence := e.resolveAzureFunctionsRate(region)
	durationS := dec(details, "duration_ms").Div(msPerS)
	memoryGB := dec(details, "memory_bytes_limit").Div(gbDecimal)
	invocations := dec(details, "invocation_count")
	cost := invocations.Mul(rate["execution_usd"]).
		Add(memoryGB.Mul(durationS).Mul(rate["gb_second_usd"]))
	return ComputeCost{CostUSD: cost, PricingSource: source, CostConfidence: confidence}
}

func (e *ComputePricingEngine) resolveAzureFunctionsRate(region string) (
	map[string]decimal.Decimal, string, string,
) {
	if block, ok := nestedMap(e.catalog, "azure", "functions_consumption"); ok {
		if regions, ok := nestedMap(block, "regions"); ok {
			if region != "" {
				if rblock, ok := nestedMap(regions, region); ok {
					return parseRateBlock(rblock, "execution_usd", "gb_second_usd"),
						fmt.Sprintf("compute_catalog:azure:functions_consumption:%s", region),
						"computed"
				}
			}
		}
		if defBlock, ok := nestedMap(block, "default"); ok {
			return parseRateBlock(defBlock, "execution_usd", "gb_second_usd"),
				"compute_catalog:azure:functions_consumption:default", "estimated"
		}
	}
	if meta, ok := nestedMap(e.catalog, "_meta"); ok {
		x, errA := decimalFromMeta(meta, "default_azure_functions_execution_usd")
		g, errB := decimalFromMeta(meta, "default_azure_functions_gb_second_usd")
		if errA == nil && errB == nil {
			return map[string]decimal.Decimal{
				"execution_usd": x,
				"gb_second_usd": g,
			}, "compute_catalog:default:azure_functions", "estimated"
		}
	}
	return hardcoded["azure_functions"], "compute_catalog:hardcoded:azure_functions", "estimated"
}

// ─── Vercel Fluid ───────────────────────────────────────────────────────

func (e *ComputePricingEngine) vercelCost(details map[string]any) ComputeCost {
	rate, source, confidence := e.resolveVercelRate()
	durationS := dec(details, "duration_ms").Div(msPerS)
	memoryGB := dec(details, "memory_bytes_limit").Div(gbDecimal)
	invocations := dec(details, "invocation_count")
	activeCPUHours := durationS.Div(hourS)
	memoryGBHours := memoryGB.Mul(durationS.Div(hourS))
	cost := invocations.Mul(rate["invocation_usd"]).
		Add(activeCPUHours.Mul(rate["active_cpu_hour_usd"])).
		Add(memoryGBHours.Mul(rate["memory_gb_hour_usd"]))
	return ComputeCost{CostUSD: cost, PricingSource: source, CostConfidence: confidence}
}

func (e *ComputePricingEngine) resolveVercelRate() (
	map[string]decimal.Decimal, string, string,
) {
	if block, ok := nestedMap(e.catalog, "vercel", "fluid"); ok {
		if defBlock, ok := nestedMap(block, "default"); ok {
			return parseRateBlock(defBlock, "active_cpu_hour_usd", "memory_gb_hour_usd", "invocation_usd"),
				"compute_catalog:vercel:fluid", "computed"
		}
	}
	if meta, ok := nestedMap(e.catalog, "_meta"); ok {
		cpu, errA := decimalFromMeta(meta, "default_vercel_cpu_hour_usd")
		mem, errB := decimalFromMeta(meta, "default_vercel_memory_gb_hour_usd")
		if errA == nil && errB == nil {
			return map[string]decimal.Decimal{
				"active_cpu_hour_usd": cpu,
				"memory_gb_hour_usd":  mem,
				"invocation_usd":      decimal.RequireFromString("0.000000600"),
			}, "compute_catalog:default:vercel", "estimated"
		}
	}
	return hardcoded["vercel_fluid"], "compute_catalog:hardcoded:vercel", "estimated"
}

// ─── EC2 / GCE / Azure VM share ─────────────────────────────────────────

func (e *ComputePricingEngine) iaasShare(
	billingModel string,
	details map[string]any,
	cloudEnv cloud.CloudEnv,
	windowS decimal.Decimal,
) ComputeCost {
	if !windowS.IsPositive() {
		windowS = dec(details, "duration_ms").Div(msPerS)
	}
	instanceType := cloudEnv.InstanceType
	region := getString(details, "region")
	hourly, source, confidence := e.resolveIaaSRate(billingModel, region, instanceType)
	vcpuCount := dec(details, "vcpu_count")
	vcpuSeconds := dec(details, "vcpu_seconds_used")
	if !vcpuCount.IsPositive() || !windowS.IsPositive() {
		return ComputeCost{CostUSD: decimal.Zero, PricingSource: source, CostConfidence: confidence}
	}
	shareFactor := vcpuSeconds.Div(vcpuCount.Mul(windowS))
	taskInstanceHours := shareFactor.Mul(windowS.Div(hourS))
	cost := taskInstanceHours.Mul(hourly)
	return ComputeCost{CostUSD: cost, PricingSource: source, CostConfidence: confidence}
}

func (e *ComputePricingEngine) resolveIaaSRate(
	billingModel, region, instanceType string,
) (decimal.Decimal, string, string) {
	providerKey, runtimeKey := map[string][2]string{
		"ec2":      {"aws", "ec2"},
		"gce":      {"gcp", "gce"},
		"azure_vm": {"azure", "vm"},
	}[billingModel][0], map[string][2]string{
		"ec2":      {"aws", "ec2"},
		"gce":      {"gcp", "gce"},
		"azure_vm": {"azure", "vm"},
	}[billingModel][1]

	if block, ok := nestedMap(e.catalog, providerKey, runtimeKey); ok {
		if regions, ok := nestedMap(block, "regions"); ok {
			if region != "" && instanceType != "" {
				if rblock, ok := nestedMap(regions, region); ok {
					if instances, ok := nestedMap(rblock, "instance_types"); ok {
						if sku, ok := nestedMap(instances, instanceType); ok {
							if h, ok := decimalFromStringField(sku, "hourly_usd"); ok {
								return h,
									fmt.Sprintf("compute_catalog:%s:%s:%s:%s",
										providerKey, runtimeKey, region, instanceType),
									"computed"
							}
						}
					}
				}
			}
		}
		if h, ok := decimalFromStringField(block, "default_vcpu_hour_usd"); ok {
			return h,
				fmt.Sprintf("compute_catalog:%s:%s:default", providerKey, runtimeKey),
				"estimated"
		}
	}
	if meta, ok := nestedMap(e.catalog, "_meta"); ok {
		if h, err := decimalFromMeta(meta, "default_ec2_vcpu_hour_usd"); err == nil {
			return h, "compute_catalog:default:" + billingModel, "estimated"
		}
	}
	return hardcoded[billingModel]["vcpu_hour_usd"],
		"compute_catalog:hardcoded:" + billingModel, "estimated"
}

// ─── K8s pod default (limits × duration × hourly) ──────────────────────

func (e *ComputePricingEngine) k8sPodLimits(details map[string]any, windowS decimal.Decimal) ComputeCost {
	if !windowS.IsPositive() {
		windowS = dec(details, "duration_ms").Div(msPerS)
	}
	rate, source, confidence := e.resolveK8sPodRate()
	vcpuCount := dec(details, "vcpu_count")
	cost := vcpuCount.Mul(windowS.Div(hourS)).Mul(rate)
	return ComputeCost{CostUSD: cost, PricingSource: source, CostConfidence: confidence}
}

func (e *ComputePricingEngine) resolveK8sPodRate() (decimal.Decimal, string, string) {
	if meta, ok := nestedMap(e.catalog, "_meta"); ok {
		if h, err := decimalFromMeta(meta, "default_k8s_pod_vcpu_hour_usd"); err == nil {
			return h, "compute_catalog:k8s_pod:limits", "computed"
		}
	}
	return hardcoded["k8s_pod"]["vcpu_hour_usd"], "compute_catalog:hardcoded:k8s_pod", "estimated"
}

// ─── Helpers ────────────────────────────────────────────────────────────

// dec parses a numeric details field to Decimal. Supports int / int64 /
// float64 / string. Panics on type mismatch — caller (ResolveComputeCost)
// wraps in defer-recover for Tier 5 fail-silent.
func dec(m map[string]any, key string) decimal.Decimal {
	v, ok := m[key]
	if !ok {
		return decimal.Zero
	}
	switch x := v.(type) {
	case nil:
		return decimal.Zero
	case int:
		return decimal.NewFromInt(int64(x))
	case int32:
		return decimal.NewFromInt(int64(x))
	case int64:
		return decimal.NewFromInt(x)
	case float64:
		return decimal.NewFromFloat(x)
	case float32:
		return decimal.NewFromFloat(float64(x))
	case string:
		// String paths: trigger Tier 5 on malformed.
		d, err := decimal.NewFromString(x)
		if err != nil {
			panic(fmt.Sprintf("decimal parse %s=%q: %v", key, x, err))
		}
		return d
	case decimal.Decimal:
		return x
	case json.Number:
		d, err := decimal.NewFromString(string(x))
		if err != nil {
			panic(fmt.Sprintf("json.Number parse %s=%q: %v", key, x, err))
		}
		return d
	}
	panic(fmt.Sprintf("unsupported numeric type for %s: %T", key, v))
}

// getString returns a string field, or "" if missing / wrong type.
func getString(m map[string]any, key string) string {
	if v, ok := m[key]; ok {
		if s, ok := v.(string); ok {
			return s
		}
	}
	return ""
}

// nestedMap walks m through a chain of keys, expecting map[string]any at
// every level. Returns (deepest, true) on success.
func nestedMap(m map[string]any, keys ...string) (map[string]any, bool) {
	if m == nil {
		return nil, false
	}
	cur := m
	for _, k := range keys {
		v, ok := cur[k]
		if !ok {
			return nil, false
		}
		next, ok := v.(map[string]any)
		if !ok {
			return nil, false
		}
		cur = next
	}
	return cur, true
}

// parseRateBlock pulls Decimal values for the named keys from a catalog
// node. Missing keys default to Decimal(0).
func parseRateBlock(m map[string]any, keys ...string) map[string]decimal.Decimal {
	out := make(map[string]decimal.Decimal, len(keys))
	for _, k := range keys {
		out[k], _ = decimalFromStringField(m, k)
	}
	return out
}

func decimalFromStringField(m map[string]any, key string) (decimal.Decimal, bool) {
	v, ok := m[key]
	if !ok {
		return decimal.Zero, false
	}
	switch x := v.(type) {
	case string:
		d, err := decimal.NewFromString(x)
		if err != nil {
			return decimal.Zero, false
		}
		return d, true
	case float64:
		return decimal.NewFromFloat(x), true
	case int:
		return decimal.NewFromInt(int64(x)), true
	case int64:
		return decimal.NewFromInt(x), true
	}
	return decimal.Zero, false
}

func decimalFromMeta(meta map[string]any, key string) (decimal.Decimal, error) {
	v, ok := decimalFromStringField(meta, key)
	if !ok {
		return decimal.Zero, fmt.Errorf("missing or unparseable: %s", key)
	}
	return v, nil
}
