// Compute pricing property invariants — spec §10.3.
//
// Must hold across arbitrary task shapes (billing_model × region ×
// architecture × duration × memory × vcpu). Table-driven.
//
// Mirrors python/tests/test_compute_invariants.py.

package pricing

import (
	"strings"
	"testing"

	"github.com/shopspring/decimal"

	"github.com/DexwoxBusiness/dexcost-go/cloud"
)

var allBillingModels = []string{
	"lambda", "fargate", "cloud_run_request", "cloud_run_instance",
	"cloud_functions", "azure_functions", "vercel_fluid",
	"ec2", "gce", "azure_vm", "k8s_pod",
}

func invariantEnv() cloud.CloudEnv {
	return cloud.CloudEnv{
		Provider: "aws", Region: "us-east-1", Source: "env",
		InstanceType: "c7g.xlarge",
	}
}

func baseDetails(billingModel string) map[string]any {
	return map[string]any{
		"billing_model":      billingModel,
		"duration_ms":        1000,
		"memory_bytes_limit": int64(512) * 1024 * 1024,
		"vcpu_count":         1.0,
		"vcpu_seconds_used":  0.5,
		"invocation_count":   1,
		"region":             "us-east-1",
		"architecture":       "x86_64",
	}
}

func TestInvariant1NeverNegative(t *testing.T) {
	eng := NewComputePricingEngine()
	for _, bm := range allBillingModels {
		t.Run(bm, func(t *testing.T) {
			cost := eng.ResolveComputeCost(baseDetails(bm), invariantEnv(), nil, decimal.NewFromInt(1))
			if cost.CostUSD.LessThan(decimal.Zero) {
				t.Fatalf("CostUSD = %s < 0 — Invariant 1 violated", cost.CostUSD)
			}
		})
	}
}

func TestInvariant3LinearityInDuration(t *testing.T) {
	eng := NewComputePricingEngine()
	for _, bm := range []string{"lambda", "azure_functions"} {
		t.Run(bm, func(t *testing.T) {
			a := baseDetails(bm)
			a["duration_ms"] = 100
			b := baseDetails(bm)
			b["duration_ms"] = 200
			costA := eng.ResolveComputeCost(a, invariantEnv(), nil, decimal.Zero)
			costB := eng.ResolveComputeCost(b, invariantEnv(), nil, decimal.Zero)
			if !costB.CostUSD.GreaterThan(costA.CostUSD) {
				t.Fatalf("Duration doubling did not increase cost: a=%s b=%s", costA.CostUSD, costB.CostUSD)
			}
		})
	}
}

func TestInvariant4ARMCheaperThanX86OnLambda(t *testing.T) {
	eng := NewComputePricingEngine()
	x86d := baseDetails("lambda")
	x86d["architecture"] = "x86_64"
	armd := baseDetails("lambda")
	armd["architecture"] = "arm64"
	x86c := eng.ResolveComputeCost(x86d, invariantEnv(), nil, decimal.Zero)
	armc := eng.ResolveComputeCost(armd, invariantEnv(), nil, decimal.Zero)
	if !armc.CostUSD.LessThan(x86c.CostUSD) {
		t.Fatalf("arm (%s) must be cheaper than x86 (%s) on Lambda", armc.CostUSD, x86c.CostUSD)
	}
}

func TestInvariant4ARMCheaperThanX86OnFargate(t *testing.T) {
	eng := NewComputePricingEngine()
	x86d := baseDetails("fargate")
	x86d["architecture"] = "x86_64"
	armd := baseDetails("fargate")
	armd["architecture"] = "arm64"
	x86c := eng.ResolveComputeCost(x86d, invariantEnv(), nil, decimal.NewFromInt(1))
	armc := eng.ResolveComputeCost(armd, invariantEnv(), nil, decimal.NewFromInt(1))
	if !armc.CostUSD.LessThan(x86c.CostUSD) {
		t.Fatalf("arm (%s) must be cheaper than x86 (%s) on Fargate", armc.CostUSD, x86c.CostUSD)
	}
}

func TestInvariant5ConfidenceIsComputedOrEstimated(t *testing.T) {
	eng := NewComputePricingEngine()
	for _, bm := range allBillingModels {
		t.Run(bm, func(t *testing.T) {
			cost := eng.ResolveComputeCost(baseDetails(bm), invariantEnv(), nil, decimal.NewFromInt(1))
			if cost.CostConfidence != "computed" && cost.CostConfidence != "estimated" {
				t.Fatalf("CostConfidence = %s, want computed|estimated", cost.CostConfidence)
			}
		})
	}
}

func TestInvariant6PricingSourceNamespace(t *testing.T) {
	eng := NewComputePricingEngine()
	for _, bm := range allBillingModels {
		t.Run(bm, func(t *testing.T) {
			cost := eng.ResolveComputeCost(baseDetails(bm), invariantEnv(), nil, decimal.NewFromInt(1))
			if !strings.HasPrefix(cost.PricingSource, "compute_catalog:") {
				t.Fatalf("PricingSource = %q, want prefix compute_catalog:", cost.PricingSource)
			}
		})
	}
}

func TestInvariant3LinearityInMemoryFargate(t *testing.T) {
	eng := NewComputePricingEngine()
	for _, gib := range []int64{1, 2, 4, 16, 64} {
		t.Run(formatGiB(gib), func(t *testing.T) {
			d := baseDetails("fargate")
			d["memory_bytes_limit"] = gib * 1024 * 1024 * 1024
			cost := eng.ResolveComputeCost(d, invariantEnv(), nil, decimal.NewFromInt(1))
			vcpuTerm := decimal.NewFromInt(1).Mul(decimal.NewFromInt(1)).
				Mul(decimal.RequireFromString("0.0000112444"))
			gibTerm := decimal.NewFromInt(gib).Mul(decimal.NewFromInt(1)).
				Mul(decimal.RequireFromString("0.0000012347"))
			expected := vcpuTerm.Add(gibTerm)
			if !cost.CostUSD.Equal(expected) {
				t.Fatalf("memory=%d GiB: cost = %s, want %s", gib, cost.CostUSD, expected)
			}
		})
	}
}

func formatGiB(n int64) string {
	switch n {
	case 1:
		return "1GiB"
	case 2:
		return "2GiB"
	case 4:
		return "4GiB"
	case 16:
		return "16GiB"
	case 64:
		return "64GiB"
	}
	return "x"
}
