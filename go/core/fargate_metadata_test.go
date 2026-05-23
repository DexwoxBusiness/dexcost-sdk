// Fargate ECS task metadata — single HTTP call per process, cached.
// Decision #7 pin: MiB → bytes via BINARY divisor (1024^3), NOT decimal GB.
//
// Mirrors python/tests/test_fargate_metadata.py.

package core

import (
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
)

func TestFargateReturnsVCPUAndMemory(t *testing.T) {
	ResetFargateMetadataForTests()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"TaskARN":"arn:aws:ecs:us-east-1:0:task/abc","Limits":{"CPU":0.5,"Memory":1024}}`))
	}))
	defer srv.Close()

	t.Setenv("ECS_CONTAINER_METADATA_URI_V4", srv.URL)
	t.Setenv("ECS_CONTAINER_METADATA_URI", "")

	m := FetchFargateMetadata()
	if m == nil {
		t.Fatal("expected metadata, got nil")
	}
	if m.VCPUCount != 0.5 {
		t.Fatalf("VCPUCount = %v, want 0.5", m.VCPUCount)
	}
	// Decision #7 — 1024 MiB → bytes via binary GiB.
	if m.MemoryBytesLimit != int64(1024)*1024*1024 {
		t.Fatalf("MemoryBytesLimit = %d, want %d (Decision #7 binary GiB)",
			m.MemoryBytesLimit, int64(1024)*1024*1024)
	}
}

func TestFargateNoEnvVarReturnsNil(t *testing.T) {
	ResetFargateMetadataForTests()
	t.Setenv("ECS_CONTAINER_METADATA_URI_V4", "")
	t.Setenv("ECS_CONTAINER_METADATA_URI", "")

	if m := FetchFargateMetadata(); m != nil {
		t.Fatalf("expected nil, got %+v", m)
	}
}

func TestFargateUnreachableReturnsNilAndLogsOnce(t *testing.T) {
	ResetFargateMetadataForTests()
	// Use a port that's guaranteed-closed quickly (close immediately).
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, "boom", http.StatusInternalServerError)
	}))
	t.Setenv("ECS_CONTAINER_METADATA_URI_V4", srv.URL)
	t.Setenv("ECS_CONTAINER_METADATA_URI", "")
	srv.Close()

	logs := captureFargateLogs(t)

	if m := FetchFargateMetadata(); m != nil {
		t.Fatalf("expected nil, got %+v", m)
	}
	if m := FetchFargateMetadata(); m != nil {
		t.Fatalf("expected nil on second call, got %+v", m)
	}
	if got := atomic.LoadInt32(logs); got != 1 {
		t.Fatalf("log count = %d, want 1 (convention §11 log-once-per-mode)", got)
	}
}

func TestFargateCachedAfterFirstSuccess(t *testing.T) {
	ResetFargateMetadataForTests()
	var hits int64
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		atomic.AddInt64(&hits, 1)
		_, _ = w.Write([]byte(`{"Limits":{"CPU":1,"Memory":512}}`))
	}))
	defer srv.Close()

	t.Setenv("ECS_CONTAINER_METADATA_URI_V4", srv.URL)
	t.Setenv("ECS_CONTAINER_METADATA_URI", "")

	a := FetchFargateMetadata()
	b := FetchFargateMetadata()
	if a == nil || b == nil {
		t.Fatal("expected non-nil")
	}
	if a != b {
		t.Fatal("expected same pointer on cached call")
	}
	if got := atomic.LoadInt64(&hits); got != 1 {
		t.Fatalf("hits = %d, want 1 (cached)", got)
	}
}

func TestFargateMalformedLimitsReturnsNil(t *testing.T) {
	ResetFargateMetadataForTests()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"Limits":{"CPU":"garbage"}}`))
	}))
	defer srv.Close()

	t.Setenv("ECS_CONTAINER_METADATA_URI_V4", srv.URL)
	t.Setenv("ECS_CONTAINER_METADATA_URI", "")

	if m := FetchFargateMetadata(); m != nil {
		t.Fatalf("expected nil, got %+v", m)
	}
}

func TestFargateV3URIAlsoWorks(t *testing.T) {
	ResetFargateMetadataForTests()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"Limits":{"CPU":2,"Memory":4096}}`))
	}))
	defer srv.Close()

	// Only the v3 (no _V4 suffix) env var set.
	t.Setenv("ECS_CONTAINER_METADATA_URI_V4", "")
	t.Setenv("ECS_CONTAINER_METADATA_URI", srv.URL)

	m := FetchFargateMetadata()
	if m == nil {
		t.Fatal("expected non-nil")
	}
	if m.VCPUCount != 2.0 {
		t.Fatalf("VCPUCount = %v, want 2.0", m.VCPUCount)
	}
	if m.MemoryBytesLimit != int64(4096)*1024*1024 {
		t.Fatalf("MemoryBytesLimit = %d, want %d", m.MemoryBytesLimit, int64(4096)*1024*1024)
	}
}

// TestFargateUsesBinaryGiBDivisor is the load-bearing Decision #7 pin —
// confusing MiB (binary) with MB (decimal) silently over-attributes
// memory cost by ~4.86% on every Fargate task.
func TestFargateUsesBinaryGiBDivisor(t *testing.T) {
	ResetFargateMetadataForTests()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"Limits":{"CPU":1,"Memory":1024}}`))
	}))
	defer srv.Close()
	t.Setenv("ECS_CONTAINER_METADATA_URI_V4", srv.URL)
	t.Setenv("ECS_CONTAINER_METADATA_URI", "")

	m := FetchFargateMetadata()
	if m == nil {
		t.Fatal("expected metadata")
	}
	if m.MemoryBytesLimit == 1_073_741_824 {
		// OK: 1024 * 1024 * 1024 — binary GiB.
		return
	}
	if m.MemoryBytesLimit == 1_000_000_000 {
		t.Fatalf("regression: Fargate using DECIMAL GB divisor (1024 MiB → %d bytes) — "+
			"Decision #7 requires BINARY divisor 1024*1024*1024 = 1_073_741_824. "+
			"This is the silent ~4.86%% over-attribution bug.", m.MemoryBytesLimit)
	}
	t.Fatalf("unexpected MemoryBytesLimit = %d", m.MemoryBytesLimit)
}
