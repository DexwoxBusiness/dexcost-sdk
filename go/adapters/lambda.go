// Package adapters provides cost-tracking adapters for common HTTP clients
// and AWS service primitives.
package adapters

import (
	"embed"
	"encoding/json"
	"fmt"
	"sort"
	"sync"

	"github.com/shopspring/decimal"
)

//go:embed data/aws_lambda_pricing.json
var embeddedLambdaPricing embed.FS

// LambdaCostResult holds the output of LambdaCost.
type LambdaCostResult struct {
	CostUSD decimal.Decimal
	Details LambdaCostDetails
}

// LambdaCostDetails breaks down the LambdaCost calculation. Mirrors the
// `details` dict returned by the Python helper at
// `adapters/aws_lambda.py:lambda_cost`.
type LambdaCostDetails struct {
	Region          string
	DurationMs      int
	MemoryMb        int
	GBSeconds       decimal.Decimal
	DurationCostUSD decimal.Decimal
	RequestCostUSD  decimal.Decimal
	RatePerGBSecond decimal.Decimal
}

type lambdaPricingEntry struct {
	DurationPerGBSecond string `json:"duration_per_gb_second"`
	RequestPerInvocation string `json:"request_per_invocation"`
}

type lambdaPricing struct {
	Regions map[string]lambdaPricingEntry `json:"regions"`
}

var (
	lambdaPricingMu   sync.RWMutex
	lambdaPricingData *lambdaPricing
)

// loadLambdaPricing parses the embedded AWS Lambda pricing JSON, caching the
// result on first use. Returns nil + error when the bundled file is missing or
// malformed.
func loadLambdaPricing() (*lambdaPricing, error) {
	lambdaPricingMu.RLock()
	cached := lambdaPricingData
	lambdaPricingMu.RUnlock()
	if cached != nil {
		return cached, nil
	}

	raw, err := embeddedLambdaPricing.ReadFile("data/aws_lambda_pricing.json")
	if err != nil {
		return nil, fmt.Errorf("adapters: read aws lambda pricing: %w", err)
	}
	var parsed lambdaPricing
	if err := json.Unmarshal(raw, &parsed); err != nil {
		return nil, fmt.Errorf("adapters: parse aws lambda pricing: %w", err)
	}

	lambdaPricingMu.Lock()
	defer lambdaPricingMu.Unlock()
	if lambdaPricingData == nil {
		lambdaPricingData = &parsed
	}
	return lambdaPricingData, nil
}

// GetSupportedLambdaRegions returns a sorted list of AWS region codes with
// bundled Lambda pricing.
func GetSupportedLambdaRegions() ([]string, error) {
	pricing, err := loadLambdaPricing()
	if err != nil {
		return nil, err
	}
	out := make([]string, 0, len(pricing.Regions))
	for k := range pricing.Regions {
		out = append(out, k)
	}
	sort.Strings(out)
	return out, nil
}

// LambdaCost calculates the cost of a single AWS Lambda invocation. Pure
// function — no I/O, no side effects. Bundled rates are read once at process
// start.
//
// Returns an error if `region` is unknown, `durationMs` < 0, or `memoryMb` <= 0.
func LambdaCost(durationMs, memoryMb int, region string) (LambdaCostResult, error) {
	if durationMs < 0 {
		return LambdaCostResult{}, fmt.Errorf("adapters: durationMs must be >= 0, got %d", durationMs)
	}
	if memoryMb <= 0 {
		return LambdaCostResult{}, fmt.Errorf("adapters: memoryMb must be > 0, got %d", memoryMb)
	}

	pricing, err := loadLambdaPricing()
	if err != nil {
		return LambdaCostResult{}, err
	}
	entry, ok := pricing.Regions[region]
	if !ok {
		regions := make([]string, 0, len(pricing.Regions))
		for k := range pricing.Regions {
			regions = append(regions, k)
		}
		sort.Strings(regions)
		return LambdaCostResult{}, fmt.Errorf(
			"adapters: unknown AWS region %q (supported: %v)", region, regions,
		)
	}

	durationSeconds := decimal.NewFromInt(int64(durationMs)).Div(decimal.NewFromInt(1000))
	memoryGB := decimal.NewFromInt(int64(memoryMb)).Div(decimal.NewFromInt(1024))
	gbSeconds := durationSeconds.Mul(memoryGB)

	ratePerGBSecond, err := decimal.NewFromString(entry.DurationPerGBSecond)
	if err != nil {
		return LambdaCostResult{}, fmt.Errorf("adapters: parse duration_per_gb_second for %s: %w", region, err)
	}
	requestCharge, err := decimal.NewFromString(entry.RequestPerInvocation)
	if err != nil {
		return LambdaCostResult{}, fmt.Errorf("adapters: parse request_per_invocation for %s: %w", region, err)
	}

	durationCost := gbSeconds.Mul(ratePerGBSecond)
	totalCost := durationCost.Add(requestCharge)

	return LambdaCostResult{
		CostUSD: totalCost,
		Details: LambdaCostDetails{
			Region:          region,
			DurationMs:      durationMs,
			MemoryMb:        memoryMb,
			GBSeconds:       gbSeconds,
			DurationCostUSD: durationCost,
			RequestCostUSD:  requestCharge,
			RatePerGBSecond: ratePerGBSecond,
		},
	}, nil
}
