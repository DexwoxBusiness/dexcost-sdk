// GPU pricing engine — Phase 2 v2. Mirrors python/src/dexcost/gpu_pricing.py.
//
// Dispatches on details.billing_model and applies the per-billing-model
// math from spec §6. Four discriminators:
//
//   - per_gpu_second_active  — Modal / RunPod / Replicate
//   - per_instance_hour      — AWS EC2 GPU / GCP GCE bundled / Azure VM GPU
//   - per_gpu_hour_reserved  — Lambda Labs / CoreWeave / GCP N1+accelerator
//   - per_vgpu_hour          — Azure NVadsA10 v5 fractional (Decision #10)
//
// Decision #7: no per-runtime memory-unit conversion table. VRAM tier is
// encoded into the SKU key.
//
// Fail-silent contract (convention §9): every code path returns a usable
// GpuCost — the five-tier degradation ladder applies (per-region exact →
// per-runtime default → device-class fallback [Decision #4] → universal
// _meta default → hardcoded constants → cost=0).
//
// Decision #1 measurement-side fallback: when details["_cgroup_scope_fallback"]
// is set, the engine appends that suffix to pricing_source and drops
// confidence to estimated.

package pricing

import (
	_ "embed"
	"encoding/json"
	"fmt"
	"log"
	"strings"
	"sync"

	"github.com/shopspring/decimal"

	"github.com/DexwoxBusiness/dexcost-sdk/go/cloud"
)

//go:embed data/gpu_prices.json
var embeddedGPUCatalogPricing []byte

// ─── Constants ──────────────────────────────────────────────────────────

var (
	gpuHourS  = decimal.NewFromInt(3600)
	gpuMSPerS = decimal.NewFromInt(1000)
)

// Tier-4 hardcoded constants. Must mirror _meta defaults in gpu_prices.json.
var gpuHardcoded = map[string]map[string]decimal.Decimal{
	"per_instance_hour":     {"hourly_usd": decimal.RequireFromString("55.04")},
	"per_gpu_second_active": {"gpu_second_usd": decimal.RequireFromString("0.000694")},
	"per_gpu_hour_reserved": {"gpu_hour_usd": decimal.RequireFromString("3.99")},
	"per_vgpu_hour":         {"vgpu_hour_usd": decimal.RequireFromString("0.454")},
}

// gpuHardcodedRateKey returns the rate-key in gpuHardcoded for a billing model.
func gpuHardcodedRateKey(billingModel string) string {
	switch billingModel {
	case "per_instance_hour":
		return "hourly_usd"
	case "per_gpu_second_active":
		return "gpu_second_usd"
	case "per_gpu_hour_reserved":
		return "gpu_hour_usd"
	case "per_vgpu_hour":
		return "vgpu_hour_usd"
	}
	return ""
}

// Decision #4 device-class default rates. Cold-start fallback for unknown
// SKUs — within ~30% of true (estimated confidence) instead of $0.
var deviceClassDefaults = map[string]map[string]decimal.Decimal{
	"hopper": {
		"per_instance_hour":     decimal.RequireFromString("98.32"),
		"per_gpu_second_active": decimal.RequireFromString("0.001097"),
		"per_gpu_hour_reserved": decimal.RequireFromString("3.99"),
		"per_vgpu_hour":         decimal.RequireFromString("3.99"),
	},
	"ampere": {
		"per_instance_hour":     decimal.RequireFromString("32.77"),
		"per_gpu_second_active": decimal.RequireFromString("0.000833"),
		"per_gpu_hour_reserved": decimal.RequireFromString("2.20"),
		"per_vgpu_hour":         decimal.RequireFromString("2.20"),
	},
	"ada_lovelace": {
		"per_instance_hour":     decimal.RequireFromString("12.00"),
		"per_gpu_second_active": decimal.RequireFromString("0.000400"),
		"per_gpu_hour_reserved": decimal.RequireFromString("1.50"),
		"per_vgpu_hour":         decimal.RequireFromString("1.50"),
	},
	"blackwell": {
		"per_instance_hour":     decimal.RequireFromString("180.00"),
		"per_gpu_second_active": decimal.RequireFromString("0.002500"),
		"per_gpu_hour_reserved": decimal.RequireFromString("6.50"),
		"per_vgpu_hour":         decimal.RequireFromString("6.50"),
	},
}

// Device-class substring patterns; most specific first.
var deviceClassPatterns = []struct {
	class    string
	patterns []string
}{
	{"blackwell", []string{"b100", "b200", "gb200", "b300", "blackwell"}},
	{"hopper", []string{"h100", "h200", "hopper"}},
	{"ada_lovelace", []string{"l4", "l40", "ada lovelace", "rtx 4090", "rtx 5090"}},
	{"ampere", []string{"a100", "a40", "a10", "ampere", "rtx 3090", "rtx a6000"}},
}

func detectDeviceClass(productNameLower string) string {
	if productNameLower == "" {
		return ""
	}
	for _, cls := range deviceClassPatterns {
		for _, p := range cls.patterns {
			if strings.Contains(productNameLower, p) {
				return cls.class
			}
		}
	}
	return ""
}

// ─── Warn-once state ────────────────────────────────────────────────────

var (
	gpuPricingWarnMu sync.Mutex
	gpuPricingWarned = map[string]struct{}{}
	gpuPricingLog    = func(format string, args ...any) {
		log.Printf("WARN dexcost.gpu_pricing: "+format, args...)
	}
)

// ResetGPUPricingWarningStateForTests clears the warned-modes set.
func ResetGPUPricingWarningStateForTests() {
	gpuPricingWarnMu.Lock()
	defer gpuPricingWarnMu.Unlock()
	gpuPricingWarned = map[string]struct{}{}
}

func gpuWarnOnce(mode, format string, args ...any) {
	gpuPricingWarnMu.Lock()
	if _, seen := gpuPricingWarned[mode]; seen {
		gpuPricingWarnMu.Unlock()
		return
	}
	gpuPricingWarned[mode] = struct{}{}
	gpuPricingWarnMu.Unlock()
	gpuPricingLog(format, args...)
}

// ─── Types ──────────────────────────────────────────────────────────────

// GpuCost is the per-event resolved GPU cost.
type GpuCost struct {
	CostUSD        decimal.Decimal
	PricingSource  string
	CostConfidence string // computed | estimated | unknown
}

// GpuPricingEngine resolves GPU cost per gpu_cost event details.
type GpuPricingEngine struct {
	catalog        map[string]any
	catalogVersion string
}

// NewGpuPricingEngine loads the embedded catalog.
func NewGpuPricingEngine() *GpuPricingEngine {
	return NewGpuPricingEngineFromBytes(embeddedGPUCatalogPricing)
}

// NewGpuPricingEngineFromBytes builds the engine from arbitrary bytes; used
// in tests to inject a malformed/partial catalog.
func NewGpuPricingEngineFromBytes(raw []byte) *GpuPricingEngine {
	e := &GpuPricingEngine{catalogVersion: "unknown"}
	var data map[string]any
	if err := json.Unmarshal(raw, &data); err != nil {
		gpuWarnOnce("gpu_catalog_malformed",
			"gpu catalog malformed JSON (%v); falling back to hardcoded", err)
		return e
	}
	e.catalog = data
	if meta, ok := data["_meta"].(map[string]any); ok {
		if v, ok := meta["version"].(string); ok {
			e.catalogVersion = v
		}
	}
	return e
}

// CatalogVersion exposes the loaded catalog's _meta.version.
func (e *GpuPricingEngine) CatalogVersion() string { return e.catalogVersion }

// ─── Public entry point — Tier-5 wrapper + Decision #1 suffix ──────────

// ResolveGPUCost computes cost for one gpu_cost event. windowS is optional;
// when zero, derived from details["duration_ms"]. Tier 5 wraps dispatch in
// defer-recover so a pricing bug cannot break task finalize.
func (e *GpuPricingEngine) ResolveGPUCost(
	details map[string]any,
	cloudEnv cloud.CloudEnv,
	windowS decimal.Decimal,
) (result GpuCost) {
	billingModel := gpuGetString(details, "billing_model")
	if billingModel == "" {
		billingModel = "unknown"
	}
	defer func() {
		if r := recover(); r != nil {
			gpuWarnOnce("gpu_pricing_failure:"+billingModel,
				"gpu pricing failed for billing_model=%s: %v; emitting cost_usd=0",
				billingModel, r)
			result = GpuCost{
				CostUSD:        decimal.Zero,
				PricingSource:  "gpu_catalog:error:" + billingModel,
				CostConfidence: "unknown",
			}
		}
		// Decision #1 _cgroup_scope_fallback suffix.
		if fb := gpuGetString(details, "_cgroup_scope_fallback"); fb != "" {
			result.PricingSource = result.PricingSource + ":" + fb
			result.CostConfidence = "estimated"
		}
	}()

	return e.dispatchGPU(billingModel, details, cloudEnv, windowS)
}

func (e *GpuPricingEngine) dispatchGPU(
	billingModel string,
	details map[string]any,
	cloudEnv cloud.CloudEnv,
	windowS decimal.Decimal,
) GpuCost {
	switch billingModel {
	case "per_gpu_second_active":
		return e.perGpuSecondActive(details, cloudEnv)
	case "per_instance_hour":
		return e.perInstanceHour(details, cloudEnv, windowS)
	case "per_gpu_hour_reserved":
		return e.perGpuHourReserved(details, cloudEnv, windowS)
	case "per_vgpu_hour":
		return e.perVgpuHour(details, cloudEnv, windowS)
	}
	gpuWarnOnce("gpu_unsupported:"+billingModel,
		"gpu pricing has no math for billing_model=%s", billingModel)
	return GpuCost{
		CostUSD:        decimal.Zero,
		PricingSource:  "gpu_catalog:unsupported:" + billingModel,
		CostConfidence: "unknown",
	}
}

// ─── per_gpu_second_active ──────────────────────────────────────────────

func (e *GpuPricingEngine) perGpuSecondActive(details map[string]any, cloudEnv cloud.CloudEnv) GpuCost {
	provider := cloudEnv.Provider
	gpuSku := gpuGetString(details, "gpu_sku")
	rate, source, confidence := e.resolvePerGpuSecondRate(provider, gpuSku, details)
	gpuSeconds := gpuDec(details, "gpu_seconds_used")
	return GpuCost{CostUSD: gpuSeconds.Mul(rate), PricingSource: source, CostConfidence: confidence}
}

func (e *GpuPricingEngine) resolvePerGpuSecondRate(
	provider, gpuSku string, details map[string]any,
) (decimal.Decimal, string, string) {
	if provider != "" && gpuSku != "" {
		if providerBlock, ok := nestedMap(e.catalog, provider, "per_gpu_second_active"); ok {
			if def, ok := nestedMap(providerBlock, "default"); ok {
				// Direct lookup (Modal, Replicate shape).
				for key, entry := range def {
					entryMap, ok := entry.(map[string]any)
					if !ok {
						continue
					}
					if sku, _ := entryMap["gpu_sku"].(string); sku == gpuSku {
						if r, ok := decimalFromStringField(entryMap, "gpu_second_usd"); ok {
							return r,
								fmt.Sprintf("gpu_catalog:%s:per_gpu_second_active:%s", provider, key),
								"computed"
						}
					}
					// Nested lookup (RunPod on_demand/community_cloud).
					for skuKey, skuEntry := range entryMap {
						skuEntryMap, ok := skuEntry.(map[string]any)
						if !ok {
							continue
						}
						if sku, _ := skuEntryMap["gpu_sku"].(string); sku == gpuSku {
							if r, ok := decimalFromStringField(skuEntryMap, "gpu_second_usd"); ok {
								return r,
									fmt.Sprintf("gpu_catalog:%s:per_gpu_second_active:%s:%s",
										provider, key, skuKey),
									"computed"
							}
						}
					}
				}
			}
		}
	}
	return e.deviceClassOrMetaFallback(details, "per_gpu_second_active", "gpu_second_usd")
}

// ─── per_instance_hour ──────────────────────────────────────────────────

func (e *GpuPricingEngine) perInstanceHour(
	details map[string]any, cloudEnv cloud.CloudEnv, windowS decimal.Decimal,
) GpuCost {
	if !windowS.IsPositive() {
		windowS = gpuDec(details, "duration_ms").Div(gpuMSPerS)
	}
	provider := cloudEnv.Provider
	region := gpuGetString(details, "region")
	instanceType := gpuGetString(details, "instance_type")
	if instanceType == "" {
		instanceType = cloudEnv.InstanceType
	}
	hourly, source, confidence := e.resolvePerInstanceRate(provider, region, instanceType, details)
	gpuCount := gpuDec(details, "gpu_count")
	gpuSeconds := gpuDec(details, "gpu_seconds_used")
	if !gpuCount.IsPositive() || !windowS.IsPositive() {
		return GpuCost{CostUSD: decimal.Zero, PricingSource: source, CostConfidence: confidence}
	}
	shareFactor := gpuSeconds.Div(gpuCount.Mul(windowS))
	taskInstanceHours := shareFactor.Mul(windowS.Div(gpuHourS))
	return GpuCost{CostUSD: taskInstanceHours.Mul(hourly), PricingSource: source, CostConfidence: confidence}
}

func (e *GpuPricingEngine) resolvePerInstanceRate(
	provider, region, instanceType string, details map[string]any,
) (decimal.Decimal, string, string) {
	blockKeys := map[string]string{
		"aws":   "ec2_gpu",
		"gcp":   "gce_gpu_bundled",
		"azure": "vm_gpu",
	}
	blockKey, ok := blockKeys[provider]
	if ok && instanceType != "" && region != "" {
		if entry, ok := nestedMap(e.catalog, provider, blockKey, "regions", region, "instance_types", instanceType); ok {
			if hourly, ok := decimalFromStringField(entry, "hourly_usd"); ok {
				return hourly,
					fmt.Sprintf("gpu_catalog:%s:%s:%s:%s", provider, blockKey, region, instanceType),
					"computed"
			}
		}
	}
	return e.deviceClassOrMetaFallback(details, "per_instance_hour", "hourly_usd")
}

// ─── per_gpu_hour_reserved ──────────────────────────────────────────────

func (e *GpuPricingEngine) perGpuHourReserved(
	details map[string]any, cloudEnv cloud.CloudEnv, windowS decimal.Decimal,
) GpuCost {
	if !windowS.IsPositive() {
		windowS = gpuDec(details, "duration_ms").Div(gpuMSPerS)
	}
	provider := cloudEnv.Provider
	gpuSku := gpuGetString(details, "gpu_sku")
	gpuHourly, source, confidence := e.resolvePerGpuHourRate(provider, gpuSku, details)
	gpuCount := gpuDec(details, "gpu_count")
	gpuSeconds := gpuDec(details, "gpu_seconds_used")
	if !gpuCount.IsPositive() || !windowS.IsPositive() {
		return GpuCost{CostUSD: decimal.Zero, PricingSource: source, CostConfidence: confidence}
	}
	shareFactor := gpuSeconds.Div(gpuCount.Mul(windowS))
	taskGpuHours := shareFactor.Mul(windowS.Div(gpuHourS)).Mul(gpuCount)
	return GpuCost{CostUSD: taskGpuHours.Mul(gpuHourly), PricingSource: source, CostConfidence: confidence}
}

func (e *GpuPricingEngine) resolvePerGpuHourRate(
	provider, gpuSku string, details map[string]any,
) (decimal.Decimal, string, string) {
	if provider != "" && gpuSku != "" {
		if block, ok := nestedMap(e.catalog, provider, "per_gpu_hour_reserved", "default"); ok {
			for key, entry := range block {
				entryMap, ok := entry.(map[string]any)
				if !ok {
					continue
				}
				if sku, _ := entryMap["gpu_sku"].(string); sku == gpuSku {
					if r, ok := decimalFromStringField(entryMap, "gpu_hour_usd"); ok {
						return r,
							fmt.Sprintf("gpu_catalog:%s:per_gpu_hour_reserved:%s", provider, key),
							"computed"
					}
				}
			}
		}
	}
	// GCP N1+accelerator path (Decision #9) — separate block.
	if provider == "gcp" && gpuSku != "" {
		region := gpuGetString(details, "region")
		if region != "" {
			if block, ok := nestedMap(e.catalog, "gcp", "gce_gpu_attached", "regions", region, "accelerator_types"); ok {
				for accKey, entry := range block {
					entryMap, ok := entry.(map[string]any)
					if !ok {
						continue
					}
					if sku, _ := entryMap["gpu_sku"].(string); sku == gpuSku {
						if r, ok := decimalFromStringField(entryMap, "gpu_hour_usd"); ok {
							return r,
								fmt.Sprintf("gpu_catalog:gcp:gce_gpu_attached:%s:%s", region, accKey),
								"computed"
						}
					}
				}
			}
		}
	}
	return e.deviceClassOrMetaFallback(details, "per_gpu_hour_reserved", "gpu_hour_usd")
}

// ─── per_vgpu_hour (Azure NVadsA10 v5 — Decision #10) ───────────────────

func (e *GpuPricingEngine) perVgpuHour(
	details map[string]any, cloudEnv cloud.CloudEnv, windowS decimal.Decimal,
) GpuCost {
	if !windowS.IsPositive() {
		windowS = gpuDec(details, "duration_ms").Div(gpuMSPerS)
	}
	provider := cloudEnv.Provider
	region := gpuGetString(details, "region")
	instanceType := gpuGetString(details, "instance_type")
	if instanceType == "" {
		instanceType = cloudEnv.InstanceType
	}
	vgpuHourly, source, confidence := e.resolvePerVgpuRate(provider, region, instanceType, details)
	gpuSeconds := gpuDec(details, "gpu_seconds_used")
	if !windowS.IsPositive() {
		return GpuCost{CostUSD: decimal.Zero, PricingSource: source, CostConfidence: confidence}
	}
	shareFactor := gpuSeconds.Div(windowS)
	taskVgpuHours := shareFactor.Mul(windowS.Div(gpuHourS))
	return GpuCost{CostUSD: taskVgpuHours.Mul(vgpuHourly), PricingSource: source, CostConfidence: confidence}
}

func (e *GpuPricingEngine) resolvePerVgpuRate(
	provider, region, instanceType string, details map[string]any,
) (decimal.Decimal, string, string) {
	if provider == "azure" && instanceType != "" && region != "" {
		if entry, ok := nestedMap(e.catalog, "azure", "vm_vgpu", "regions", region, "instance_types", instanceType); ok {
			if rate, ok := decimalFromStringField(entry, "vgpu_hour_usd"); ok {
				return rate,
					fmt.Sprintf("gpu_catalog:azure:vm_vgpu:%s:%s", region, instanceType),
					"computed"
			}
		}
	}
	return e.deviceClassOrMetaFallback(details, "per_vgpu_hour", "vgpu_hour_usd")
}

// ─── Tier-3/4 fallback ladder ──────────────────────────────────────────

// deviceClassOrMetaFallback walks Tier-3a (device-class) → Tier-3b (_meta
// default) → Tier-4 (hardcoded constants). Always succeeds.
func (e *GpuPricingEngine) deviceClassOrMetaFallback(
	details map[string]any, billingModel, rateKey string,
) (decimal.Decimal, string, string) {
	// Tier-3a: device-class fallback via productName substring matching.
	productName := gpuGetString(details, "_nvml_product_name_lower")
	if deviceClass := detectDeviceClass(productName); deviceClass != "" {
		if rate, ok := deviceClassDefaults[deviceClass][billingModel]; ok {
			gpuWarnOnce("gpu_sku_unknown:"+productName,
				"GPU SKU not in catalog (productName=%q); falling back to device_class=%s default rate (~30%% accuracy band)",
				productName, deviceClass)
			return rate,
				fmt.Sprintf("gpu_catalog:device_class_fallback:%s:%s", deviceClass, billingModel),
				"estimated"
		}
	}
	// Tier-3b: universal _meta default.
	if meta, ok := nestedMap(e.catalog, "_meta"); ok {
		metaKey := "default_" + billingModel + "_usd"
		if rate, ok := decimalFromStringField(meta, metaKey); ok {
			return rate, "gpu_catalog:default:" + billingModel, "estimated"
		}
	}
	// Tier-4: hardcoded.
	return gpuHardcoded[billingModel][rateKey],
		"gpu_catalog:hardcoded:" + billingModel,
		"estimated"
}

// ─── Helpers ────────────────────────────────────────────────────────────

func gpuGetString(m map[string]any, k string) string {
	if v, ok := m[k]; ok {
		if s, ok := v.(string); ok {
			return s
		}
	}
	return ""
}

// gpuDec parses a numeric details field to Decimal. Supports int / int64 /
// float64 / string. Panics on type mismatch — caller wraps with Tier-5 recover.
func gpuDec(m map[string]any, key string) decimal.Decimal {
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
		d, err := decimal.NewFromString(x)
		if err != nil {
			panic(fmt.Sprintf("gpu_pricing: decimal parse %s=%q: %v", key, x, err))
		}
		return d
	case decimal.Decimal:
		return x
	case json.Number:
		d, err := decimal.NewFromString(string(x))
		if err != nil {
			panic(fmt.Sprintf("gpu_pricing: json.Number parse %s=%q: %v", key, x, err))
		}
		return d
	}
	panic(fmt.Sprintf("gpu_pricing: unsupported numeric type for %s: %T", key, v))
}
