// Package core — NetworkAccountant ports python/src/dexcost/network_
// accountant.py to Go. One instance lives per task as an unserialized
// in-process accumulator. The HTTP RoundTripper (in adapters/) calls
// Record per call via the registry below; Finalize is called once at
// task end by the tracker. After finalize the accountant is frozen —
// later Record calls are no-ops, so late-arriving bytes never mutate
// already-shipped task aggregates.
//
// Lives in core rather than adapters because the tracker (also in
// core) needs to reference it on task start/end, and core cannot
// import adapters (that'd be a circular import — adapters imports
// core). Mirrors Python's top-level placement of this file.
package core

import (
	"sort"
	"sync"
)

// FinalizeCap is the number of host entries kept in by_host after finalize
// (plus _other). Mirrors python FINALIZE_CAP.
const FinalizeCap = 20

// LiveCap is the maximum distinct hosts tracked live before overflow folds
// into _other. Bounds mid-task memory for pathological many-host workloads.
const LiveCap = 500

// NetworkAccountant accumulates HTTP byte usage for a single tracked task.
//
// isInternal follows the v1 §4.2 three-valued classification (mirrors the
// netbytes.ClassifyDestination return type):
//
//   - *bool == true  → bytes are intra-VPC / loopback → 0 external bytes.
//   - *bool == false → confirmed public IP → all of bytesOut are external.
//   - nil            → unresolved named host → treated as external
//     (conservative — over-attribute rather than undercount).
type NetworkAccountant struct {
	mu                sync.Mutex
	bytesIn           int64
	bytesOut          int64
	externalBytesOut  int64
	callCount         int64
	hosts             map[string][4]int64 // host → [calls, bytes_in, bytes_out, external_bytes_out]
	other             [4]int64
	frozen            bool
}

// NewNetworkAccountant returns an empty, ready-to-use accountant.
func NewNetworkAccountant() *NetworkAccountant {
	return &NetworkAccountant{hosts: make(map[string][4]int64)}
}

// Record adds one HTTP call's bytes. No-op once finalized.
func (a *NetworkAccountant) Record(host string, bytesIn, bytesOut int64, isInternal *bool) {
	// Clamp negatives — bytes can never be negative.
	if bytesIn < 0 {
		bytesIn = 0
	}
	if bytesOut < 0 {
		bytesOut = 0
	}
	externalOut := bytesOut
	if isInternal != nil && *isInternal {
		externalOut = 0
	}

	a.mu.Lock()
	defer a.mu.Unlock()
	if a.frozen {
		return
	}
	a.bytesIn += bytesIn
	a.bytesOut += bytesOut
	a.externalBytesOut += externalOut
	a.callCount++

	key := host
	if key == "" {
		key = "_unknown"
	}

	if entry, exists := a.hosts[key]; exists {
		entry[0]++
		entry[1] += bytesIn
		entry[2] += bytesOut
		entry[3] += externalOut
		a.hosts[key] = entry
	} else if len(a.hosts) < LiveCap {
		a.hosts[key] = [4]int64{1, bytesIn, bytesOut, externalOut}
	} else {
		a.other[0]++
		a.other[1] += bytesIn
		a.other[2] += bytesOut
		a.other[3] += externalOut
	}
}

// LiveHostCount returns the number of distinct hosts currently tracked
// (excludes the synthetic _other bucket).
func (a *NetworkAccountant) LiveHostCount() int {
	a.mu.Lock()
	defer a.mu.Unlock()
	return len(a.hosts)
}

// NetworkSnapshot is the payload returned by Finalize.
type NetworkSnapshot struct {
	BytesIn          int64
	BytesOut         int64
	ExternalBytesOut int64 // canonical scalar — basis for v2 network_cost_usd
	CallCount        int64
	// ByHost is shaped as {"hosts": [...]} where each entry is
	// {host, calls, bytes_in, bytes_out, external_bytes_out}.
	ByHost map[string]interface{}
}

type hostEntry struct {
	host   string
	values [4]int64
}

// Finalize freezes the accountant and returns the snapshot for the task fields.
//
// ByHost contains the top FinalizeCap hosts by total bytes (bytes_in +
// bytes_out), plus an _other bucket summing the rest. Each host entry
// carries external_bytes_out so v2 per-host egress cost survives the cap.
// If a real host is literally named "_other" it is folded into the
// synthetic overflow bucket — the output never has two entries with the
// same name.
func (a *NetworkAccountant) Finalize() NetworkSnapshot {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.frozen = true

	// Drain hosts into a slice we can sort by (bytes_in + bytes_out) desc.
	ranked := make([]hostEntry, 0, len(a.hosts))
	for host, vals := range a.hosts {
		ranked = append(ranked, hostEntry{host: host, values: vals})
	}
	// Stable insertion-order tie-break isn't guaranteed (map iteration is
	// random), but ties between hosts with identical totals are not a
	// correctness issue — they get the same surfacing in the ranking.
	sort.Slice(ranked, func(i, j int) bool {
		totalI := ranked[i].values[1] + ranked[i].values[2]
		totalJ := ranked[j].values[1] + ranked[j].values[2]
		return totalI > totalJ
	})

	other := a.other
	top := make([]hostEntry, 0, FinalizeCap)
	for idx, item := range ranked {
		if idx < FinalizeCap {
			// Fold a real host literally named "_other" into the synthetic
			// bucket so the output never contains a duplicate.
			if item.host == "_other" {
				for i := 0; i < 4; i++ {
					other[i] += item.values[i]
				}
			} else {
				top = append(top, item)
			}
		} else {
			for i := 0; i < 4; i++ {
				other[i] += item.values[i]
			}
		}
	}

	hosts := make([]map[string]interface{}, 0, len(top)+1)
	for _, e := range top {
		hosts = append(hosts, map[string]interface{}{
			"host":               e.host,
			"calls":              e.values[0],
			"bytes_in":           e.values[1],
			"bytes_out":          e.values[2],
			"external_bytes_out": e.values[3],
		})
	}
	if other[0] > 0 {
		hosts = append(hosts, map[string]interface{}{
			"host":               "_other",
			"calls":              other[0],
			"bytes_in":           other[1],
			"bytes_out":          other[2],
			"external_bytes_out": other[3],
		})
	}

	return NetworkSnapshot{
		BytesIn:          a.bytesIn,
		BytesOut:         a.bytesOut,
		ExternalBytesOut: a.externalBytesOut,
		CallCount:        a.callCount,
		ByHost:           map[string]interface{}{"hosts": hosts},
	}
}

// ---------------------------------------------------------------------------
// Registry — task_id → *NetworkAccountant
// ---------------------------------------------------------------------------
//
// The HTTP RoundTripper sees a task_id (via context resolution) but doesn't
// carry a reference to the Task struct. This registry maps task_id strings
// to accountants so the adapter can record bytes via lookup. The tracker
// is responsible for registering on task start and unregistering on
// finalize (Phase D Task 10). Mirrors rust/src/adapters/network_accountant.rs.

var (
	accountantRegistryMu sync.RWMutex
	accountantRegistry   = map[string]*NetworkAccountant{}
)

// RegisterAccountant registers a task's accountant so the HTTP adapter can
// find it by task_id. Replaces any prior registration for the same id.
func RegisterAccountant(taskID string, accountant *NetworkAccountant) {
	accountantRegistryMu.Lock()
	defer accountantRegistryMu.Unlock()
	accountantRegistry[taskID] = accountant
}

// GetAccountant resolves a task's accountant by task_id. Returns nil when
// no task with that id has been registered.
func GetAccountant(taskID string) *NetworkAccountant {
	accountantRegistryMu.RLock()
	defer accountantRegistryMu.RUnlock()
	return accountantRegistry[taskID]
}

// UnregisterAccountant removes and returns a task's accountant. Called by
// the tracker at task end after Finalize() has snapshotted the bytes onto
// the task. Idempotent — returns nil if already removed.
func UnregisterAccountant(taskID string) *NetworkAccountant {
	accountantRegistryMu.Lock()
	defer accountantRegistryMu.Unlock()
	a := accountantRegistry[taskID]
	delete(accountantRegistry, taskID)
	return a
}

// ResetAccountantRegistryForTests clears the entire registry. Exported
// (with the explicit `ForTests` suffix) so cross-package tests in the
// adapters and integration suites can isolate themselves.
func ResetAccountantRegistryForTests() {
	accountantRegistryMu.Lock()
	defer accountantRegistryMu.Unlock()
	accountantRegistry = map[string]*NetworkAccountant{}
}

