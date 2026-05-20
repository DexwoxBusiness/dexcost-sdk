// Fargate ECS task metadata reader.
//
// Hits ${ECS_CONTAINER_METADATA_URI_V4}/task (or v3) once per process and
// caches the parsed result. Exposes VCPUCount (float64) and
// MemoryBytesLimit (int64 — converted from MiB per Decision #7).
//
// Fail-silent contract (convention §9): unreachable endpoint, malformed
// JSON, missing fields all return nil and log once via convention §11.
//
// Mirrors python/src/dexcost/fargate_metadata.py.

package core

import (
	"context"
	"encoding/json"
	"io"
	"log"
	"net/http"
	"os"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

const fargateProbeTimeout = 250 * time.Millisecond

// FargateTaskMetadata is the parsed Limits block.
type FargateTaskMetadata struct {
	VCPUCount        float64
	MemoryBytesLimit int64
}

var (
	fargateMu       sync.Mutex
	fargateCached   *FargateTaskMetadata
	fargateResolved bool
	fargateWarnLogs int32 // atomic counter, exposed via captureFargateLogs

	// fargateLogf is the warn-once logger. Overridable in tests so we can
	// count emissions without scraping stderr.
	fargateLogf = func(format string, args ...any) {
		log.Printf("WARN dexcost.fargate: "+format, args...)
	}
)

// ResetFargateMetadataForTests clears cached state. Test-only helper per
// convention §11.
func ResetFargateMetadataForTests() {
	fargateMu.Lock()
	defer fargateMu.Unlock()
	fargateCached = nil
	fargateResolved = false
	atomic.StoreInt32(&fargateWarnLogs, 0)
}

// captureFargateLogs swaps fargateLogf with a counter-incrementer for the
// duration of the test. Returns the counter pointer.
func captureFargateLogs(t interface{ Cleanup(func()) }) *int32 {
	old := fargateLogf
	fargateLogf = func(format string, args ...any) {
		atomic.AddInt32(&fargateWarnLogs, 1)
	}
	t.Cleanup(func() { fargateLogf = old })
	return &fargateWarnLogs
}

func fargateEndpoint() string {
	base := os.Getenv("ECS_CONTAINER_METADATA_URI_V4")
	if base == "" {
		base = os.Getenv("ECS_CONTAINER_METADATA_URI")
	}
	if base == "" {
		return ""
	}
	return strings.TrimRight(base, "/") + "/task"
}

// FetchFargateMetadata reads + caches the ECS task metadata. Idempotent.
// Returns nil when not on Fargate / unreachable / malformed.
func FetchFargateMetadata() *FargateTaskMetadata {
	fargateMu.Lock()
	if fargateResolved {
		cached := fargateCached
		fargateMu.Unlock()
		return cached
	}
	fargateMu.Unlock()

	url := fargateEndpoint()
	if url == "" {
		fargateMu.Lock()
		fargateResolved = true
		fargateMu.Unlock()
		return nil
	}

	ctx, cancel := context.WithTimeout(context.Background(), fargateProbeTimeout)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		fargateMu.Lock()
		fargateResolved = true
		warn := !atomic.CompareAndSwapInt32(&fargateWarnLogs, 0, 0) // peek
		_ = warn
		if atomic.LoadInt32(&fargateWarnLogs) == 0 {
			fargateLogf("metadata unreachable (%v); compute cost will fall through to default rates", err)
		}
		fargateMu.Unlock()
		return nil
	}
	client := &http.Client{Timeout: fargateProbeTimeout}
	resp, err := client.Do(req)
	if err != nil {
		fargateMu.Lock()
		fargateResolved = true
		if atomic.LoadInt32(&fargateWarnLogs) == 0 {
			fargateLogf("metadata unreachable (%v); compute cost will fall through to default rates", err)
		}
		fargateMu.Unlock()
		return nil
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		fargateMu.Lock()
		fargateResolved = true
		if atomic.LoadInt32(&fargateWarnLogs) == 0 {
			fargateLogf("metadata non-2xx status %d; compute cost will fall through to default rates", resp.StatusCode)
		}
		fargateMu.Unlock()
		return nil
	}
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		fargateMu.Lock()
		fargateResolved = true
		if atomic.LoadInt32(&fargateWarnLogs) == 0 {
			fargateLogf("metadata read error (%v); compute cost will fall through to default rates", err)
		}
		fargateMu.Unlock()
		return nil
	}

	var payload struct {
		Limits struct {
			// CPU may be a float (e.g. 0.5) or an int (e.g. 2). json.Number
			// preserves both; we parse to float64 below. Memory is always int.
			CPU    json.Number `json:"CPU"`
			Memory json.Number `json:"Memory"`
		} `json:"Limits"`
	}
	dec := json.NewDecoder(strings.NewReader(string(body)))
	dec.UseNumber()
	if err := dec.Decode(&payload); err != nil {
		fargateMu.Lock()
		fargateResolved = true
		fargateMu.Unlock()
		return nil
	}

	vcpu, err := payload.Limits.CPU.Float64()
	if err != nil {
		fargateMu.Lock()
		fargateResolved = true
		fargateMu.Unlock()
		return nil
	}
	memMiB, err := payload.Limits.Memory.Int64()
	if err != nil {
		fargateMu.Lock()
		fargateResolved = true
		fargateMu.Unlock()
		return nil
	}

	// Decision #7 — Fargate memory is in MiB (binary), NOT MB. Convert to
	// bytes via the binary divisor (~4.86% silent over-attribution bug if
	// decimal MB is used by mistake). This single line is load-bearing.
	memoryBytes := memMiB * 1024 * 1024

	result := &FargateTaskMetadata{
		VCPUCount:        vcpu,
		MemoryBytesLimit: memoryBytes,
	}
	fargateMu.Lock()
	fargateCached = result
	fargateResolved = true
	fargateMu.Unlock()
	return result
}
