package scanner

import (
	"os"
	"path/filepath"
	"testing"
)

// helper: write a Go source file to a temp directory and return the path
func writeTempGoFile(t *testing.T, dir, filename, content string) string {
	t.Helper()
	path := filepath.Join(dir, filename)
	if err := os.WriteFile(path, []byte(content), 0644); err != nil {
		t.Fatalf("failed to write temp file: %v", err)
	}
	return path
}

func TestScanDirectory_DetectsOpenAI(t *testing.T) {
	dir := t.TempDir()
	writeTempGoFile(t, dir, "agent.go", `
package main

import (
	openai "github.com/sashabaranov/go-openai"
)

func run() {
	client := openai.NewClient("key")
	client.CreateChatCompletion(nil, openai.ChatCompletionRequest{})
}
`)

	result := ScanDirectory(dir)
	if result.FilesScanned != 1 {
		t.Errorf("expected 1 file scanned, got %d", result.FilesScanned)
	}
	if len(result.CostPoints) == 0 {
		t.Error("expected at least 1 cost point for OpenAI, got 0")
	}

	found := false
	for _, cp := range result.CostPoints {
		if cp.Provider == "openai" {
			found = true
			break
		}
	}
	if !found {
		t.Error("expected an OpenAI cost point")
	}
}

func TestScanDirectory_DetectsAnthropic(t *testing.T) {
	dir := t.TempDir()
	writeTempGoFile(t, dir, "bot.go", `
package main

import (
	anthropic "github.com/anthropics/anthropic-sdk-go"
)

func run() {
	client := anthropic.NewClient()
	client.Messages.Create(nil)
}
`)

	result := ScanDirectory(dir)
	if result.FilesScanned != 1 {
		t.Errorf("expected 1 file scanned, got %d", result.FilesScanned)
	}
	// Should detect the anthropic import at minimum
	foundImport := false
	for _, cp := range result.CostPoints {
		if cp.Provider == "anthropic" {
			foundImport = true
			break
		}
	}
	if !foundImport {
		t.Error("expected an Anthropic cost point from import detection")
	}
}

func TestScanDirectory_DetectsHTTPCalls(t *testing.T) {
	dir := t.TempDir()
	writeTempGoFile(t, dir, "fetch.go", `
package main

import "net/http"

func fetch() {
	http.Get("https://api.example.com/data")
}
`)

	result := ScanDirectory(dir)
	if result.FilesScanned != 1 {
		t.Errorf("expected 1 file scanned, got %d", result.FilesScanned)
	}

	found := false
	for _, cp := range result.CostPoints {
		if cp.Category == "http" && cp.Provider == "net/http" {
			found = true
			break
		}
	}
	if !found {
		t.Error("expected an HTTP cost point")
	}
}

func TestScanDirectory_DetectsMultipleProviders(t *testing.T) {
	dir := t.TempDir()
	writeTempGoFile(t, dir, "multi.go", `
package main

import (
	openai "github.com/sashabaranov/go-openai"
	"github.com/pinecone-io/go-pinecone/pinecone"
	"net/http"
)

func run() {
	openai.NewClient("key")
	pinecone.NewClient()
	http.Get("https://example.com")
}
`)

	result := ScanDirectory(dir)
	if result.FilesScanned != 1 {
		t.Errorf("expected 1 file scanned, got %d", result.FilesScanned)
	}

	providers := make(map[string]bool)
	for _, cp := range result.CostPoints {
		providers[cp.Provider] = true
	}

	if !providers["openai"] {
		t.Error("expected openai provider detected")
	}
	if !providers["net/http"] {
		t.Error("expected net/http provider detected")
	}
}

func TestScanDirectory_SkipsTestFiles(t *testing.T) {
	dir := t.TempDir()
	writeTempGoFile(t, dir, "agent_test.go", `
package main

import (
	openai "github.com/sashabaranov/go-openai"
)

func TestRun() {
	openai.NewClient("key")
}
`)

	result := ScanDirectory(dir)
	if result.FilesScanned != 0 {
		t.Errorf("expected 0 files scanned (test files skipped), got %d", result.FilesScanned)
	}
	if len(result.CostPoints) != 0 {
		t.Errorf("expected 0 cost points from test files, got %d", len(result.CostPoints))
	}
}

func TestScanDirectory_SkipsVendor(t *testing.T) {
	dir := t.TempDir()
	vendorDir := filepath.Join(dir, "vendor")
	if err := os.MkdirAll(vendorDir, 0755); err != nil {
		t.Fatal(err)
	}
	writeTempGoFile(t, vendorDir, "dep.go", `
package dep

import openai "github.com/sashabaranov/go-openai"

func init() { openai.NewClient("key") }
`)

	result := ScanDirectory(dir)
	if result.FilesScanned != 0 {
		t.Errorf("expected 0 files scanned (vendor skipped), got %d", result.FilesScanned)
	}
}

func TestScanDirectory_EmptyDirectory(t *testing.T) {
	dir := t.TempDir()
	result := ScanDirectory(dir)
	if result.FilesScanned != 0 {
		t.Errorf("expected 0 files scanned, got %d", result.FilesScanned)
	}
	if len(result.CostPoints) != 0 {
		t.Errorf("expected 0 cost points, got %d", len(result.CostPoints))
	}
}

func TestScanDirectory_NonexistentPath(t *testing.T) {
	result := ScanDirectory("/nonexistent/path/that/does/not/exist")
	if result.FilesScanned != 0 {
		t.Errorf("expected 0 files scanned for nonexistent path, got %d", result.FilesScanned)
	}
}

func TestScanResult_AutoAndManualCount(t *testing.T) {
	r := ScanResult{
		CostPoints: []CostPoint{
			{AutoInstrumented: true},
			{AutoInstrumented: true},
			{AutoInstrumented: false},
			{AutoInstrumented: false},
			{AutoInstrumented: false},
		},
	}
	if r.AutoCount() != 2 {
		t.Errorf("expected AutoCount=2, got %d", r.AutoCount())
	}
	if r.ManualCount() != 3 {
		t.Errorf("expected ManualCount=3, got %d", r.ManualCount())
	}
}

func TestScanDirectory_DetectsLangChainGo(t *testing.T) {
	dir := t.TempDir()
	writeTempGoFile(t, dir, "chain.go", `
package main

import (
	"github.com/tmc/langchaingo/llms"
)

func run() {
	llms.Call(nil, "prompt")
}
`)

	result := ScanDirectory(dir)
	found := false
	for _, cp := range result.CostPoints {
		if cp.Provider == "langchaingo" && cp.Category == "framework" {
			found = true
			break
		}
	}
	if !found {
		t.Error("expected a LangChainGo framework cost point")
	}
}

func TestScanDirectory_DetectsVectorDB(t *testing.T) {
	dir := t.TempDir()
	writeTempGoFile(t, dir, "search.go", `
package main

import (
	"github.com/pinecone-io/go-pinecone/pinecone"
)

func search() {
	_ = pinecone.NewClient()
}
`)

	result := ScanDirectory(dir)
	foundPinecone := false
	for _, cp := range result.CostPoints {
		if cp.Provider == "pinecone" {
			foundPinecone = true
			break
		}
	}
	if !foundPinecone {
		t.Error("expected a Pinecone cost point from import detection")
	}
}

// --- GenerateStubs tests ---

func TestGenerateStubs_AutoOnlyOmitsDecimalImport(t *testing.T) {
	result := ScanResult{
		CostPoints: []CostPoint{
			{Category: "llm", Provider: "openai", Description: "OpenAI chat completion", AutoInstrumented: true},
		},
	}
	stubs := GenerateStubs(result)
	if contains(stubs, "shopspring/decimal") {
		t.Error("auto-only stubs should not import shopspring/decimal")
	}
	if !contains(stubs, "dexcost \"github.com/DexwoxBusiness/dexcost-sdk/go\"") {
		t.Error("stubs must import dexcost")
	}
	if !contains(stubs, "Auto-instrumented") {
		t.Error("auto-only stubs should list auto-instrumented providers")
	}
}

func TestGenerateStubs_ManualIncludesDecimalImport(t *testing.T) {
	result := ScanResult{
		CostPoints: []CostPoint{
			{Category: "service", Provider: "stripe", Description: "Stripe API call", AutoInstrumented: false, File: "main.go", Line: 42},
		},
	}
	stubs := GenerateStubs(result)
	if !contains(stubs, "shopspring/decimal") {
		t.Error("manual stubs must import shopspring/decimal")
	}
	if !contains(stubs, "task.RecordCost(\"stripe\"") {
		t.Error("manual stubs must contain RecordCost call for stripe")
	}
	if !contains(stubs, "main.go:42 — Stripe API call") {
		t.Error("manual stubs must contain file/line comment")
	}
}

func TestGenerateStubs_MixedAutoAndManual(t *testing.T) {
	result := ScanResult{
		CostPoints: []CostPoint{
			{Category: "llm", Provider: "openai", Description: "OpenAI chat completion", AutoInstrumented: true},
			{Category: "service", Provider: "stripe", Description: "Stripe API call", AutoInstrumented: false, File: "main.go", Line: 10},
			{Category: "http", Provider: "net/http", Description: "HTTP GET", AutoInstrumented: false, File: "client.go", Line: 20},
		},
	}
	stubs := GenerateStubs(result)
	if !contains(stubs, "shopspring/decimal") {
		t.Error("mixed stubs must import shopspring/decimal when manual points exist")
	}
	if !contains(stubs, "Auto-instrumented") {
		t.Error("mixed stubs should still list auto-instrumented providers")
	}
	if !contains(stubs, "Manual cost tracking") {
		t.Error("mixed stubs should contain manual cost tracking section")
	}
	// Verify pluralS: 1 call should not have trailing 's'
	if contains(stubs, "openai (1 calls)") {
		t.Error("pluralS should omit 's' for count == 1")
	}
	if !contains(stubs, "openai (1 call detected)") {
		t.Error("expected singular 'call' for count == 1")
	}
}

func TestGenerateStubs_EmptyResult(t *testing.T) {
	result := ScanResult{CostPoints: []CostPoint{}}
	stubs := GenerateStubs(result)
	if contains(stubs, "shopspring/decimal") {
		t.Error("empty result should not import decimal")
	}
	if contains(stubs, "Auto-instrumented") {
		t.Error("empty result should not list auto-instrumented section")
	}
	if contains(stubs, "Manual cost tracking") {
		t.Error("empty result should not list manual tracking section")
	}
}

func TestGenerateStubs_SkipsImportCategory(t *testing.T) {
	result := ScanResult{
		CostPoints: []CostPoint{
			{Category: "import", Provider: "openai", Description: "Import: github.com/sashabaranov/go-openai", AutoInstrumented: false},
			{Category: "llm", Provider: "openai", Description: "OpenAI chat completion", AutoInstrumented: true},
		},
	}
	stubs := GenerateStubs(result)
	if contains(stubs, "Import:") {
		t.Error("import-category cost points should be skipped in stubs")
	}
}

func TestPluralS(t *testing.T) {
	if pluralS(0) != "s" {
		t.Errorf("pluralS(0) = %q, want \"s\"", pluralS(0))
	}
	if pluralS(1) != "" {
		t.Errorf("pluralS(1) = %q, want \"\"", pluralS(1))
	}
	if pluralS(2) != "s" {
		t.Errorf("pluralS(2) = %q, want \"s\"", pluralS(2))
	}
}

func contains(s, substr string) bool {
	return len(s) >= len(substr) && (s == substr || len(s) > 0 && containsImpl(s, substr))
}

func containsImpl(s, substr string) bool {
	for i := 0; i <= len(s)-len(substr); i++ {
		if s[i:i+len(substr)] == substr {
			return true
		}
	}
	return false
}
