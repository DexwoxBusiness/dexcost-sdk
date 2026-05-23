// Cgroup-scope classifier — Phase 2 GPU foundation Decision #1.
//
// Mirrors python/src/dexcost/cgroup_walker.py.
//
// Reads /proc/self/cgroup and classifies the cgroup scope by prefix:
//
//   - "container" — kubepods.slice/kubepods/docker/system.slice/docker-/
//     containerd/system.slice/containerd-/crio/system.slice/crio-.
//     The dexcost-task's cgroup IS the scope; walking cgroup.procs
//     enumerates exactly the container's PIDs.
//   - "bare_metal_user_slice" — /user.slice/... (systemd user session).
//     Walking this would capture every PID in the SSH/login session, not
//     just dexcost's task. Degrade to self-PID-only at estimated
//     confidence with pricing_source ":no_container_scope".
//   - "root_cgroup" — "/" (privileged single-tenant host). Ambiguous;
//     degrade to self-PID-only.
//   - "cgroup_v1" — multi-line file (multiple controllers); v1 degrades
//     to self-PID-only.
//   - "unknown" — anything else.

package core

import (
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
)

// CgroupKind names the classifier output. Values match Python.
type CgroupKind string

const (
	CgroupKindContainer          CgroupKind = "container"
	CgroupKindBareMetalUserSlice CgroupKind = "bare_metal_user_slice"
	CgroupKindRootCgroup         CgroupKind = "root_cgroup"
	CgroupKindCgroupV1           CgroupKind = "cgroup_v1"
	CgroupKindUnknown            CgroupKind = "unknown"
)

// CgroupScope is the classified cgroup scope. Path is set only when
// Kind == container (it's the cgroup-v2 unified path).
type CgroupScope struct {
	Kind CgroupKind
	Path string
}

// containerPrefixes is the Decision #1 classification table.
var containerPrefixes = []string{
	"/kubepods.slice/",
	"/kubepods/",
	"/docker/",
	"/system.slice/docker-",
	"/containerd/",
	"/system.slice/containerd-",
	"/crio/",
	"/system.slice/crio-",
}

var bareMetalPrefixes = []string{
	"/user.slice/",
}

// procSelfCgroup is the default path used by ClassifyCgroupScope. Tests
// invoke classifyCgroupScopeFromFile directly.
const procSelfCgroup = "/proc/self/cgroup"

// ClassifyCgroupScope reads the live /proc/self/cgroup.
func ClassifyCgroupScope() CgroupScope {
	return classifyCgroupScopeFromFile(procSelfCgroup)
}

// classifyCgroupScopeFromFile is the testable inner.
func classifyCgroupScopeFromFile(path string) CgroupScope {
	raw, err := os.ReadFile(path)
	if err != nil {
		return CgroupScope{Kind: CgroupKindUnknown}
	}
	var lines []string
	for _, ln := range strings.Split(string(raw), "\n") {
		ln = strings.TrimSpace(ln)
		if ln != "" {
			lines = append(lines, ln)
		}
	}
	if len(lines) == 0 {
		return CgroupScope{Kind: CgroupKindUnknown}
	}
	// cgroup v1 → multiple controller lines; v2 → single line "0::/path".
	if len(lines) > 1 || !strings.HasPrefix(lines[0], "0::") {
		return CgroupScope{Kind: CgroupKindCgroupV1}
	}
	p := lines[0][3:]
	if p == "/" || p == "" {
		return CgroupScope{Kind: CgroupKindRootCgroup}
	}
	for _, prefix := range containerPrefixes {
		if strings.HasPrefix(p, prefix) {
			return CgroupScope{Kind: CgroupKindContainer, Path: p}
		}
	}
	for _, prefix := range bareMetalPrefixes {
		if strings.HasPrefix(p, prefix) {
			return CgroupScope{Kind: CgroupKindBareMetalUserSlice}
		}
	}
	return CgroupScope{Kind: CgroupKindUnknown}
}

// cgroupV2Root is the cgroup v2 mount point. Default is /sys/fs/cgroup;
// tests pass a temp dir via the cgroupRoot arg to EnumerateCgroupPIDs.
const defaultCgroupV2Root = "/sys/fs/cgroup"

var (
	cgroupWalkerWarnMu sync.Mutex
	cgroupWalkerWarned = map[string]struct{}{}
)

func cgroupWalkerWarnOnce(mode string) {
	cgroupWalkerWarnMu.Lock()
	defer cgroupWalkerWarnMu.Unlock()
	if _, seen := cgroupWalkerWarned[mode]; seen {
		return
	}
	cgroupWalkerWarned[mode] = struct{}{}
}

// EnumerateCgroupPIDs returns the PID set to attribute GPU usage to.
//
//   - For container scope: walks cgroupRoot + scope.Path + cgroup.procs.
//     If cgroupRoot is "", the default mount is used.
//   - For every non-container scope: returns []int{os.Getpid()}.
//   - Returns nil when the container's cgroup.procs is unreadable (signals
//     the caller to log-once + degrade).
func EnumerateCgroupPIDs(scope CgroupScope, cgroupRoot string) []int {
	if scope.Kind != CgroupKindContainer || scope.Path == "" {
		return []int{os.Getpid()}
	}
	root := cgroupRoot
	if root == "" {
		root = defaultCgroupV2Root
	}
	procsPath := filepath.Join(root+scope.Path, "cgroup.procs")
	raw, err := os.ReadFile(procsPath)
	if err != nil {
		cgroupWalkerWarnOnce("gpu_cgroup_walk_forbidden")
		return nil
	}
	var pids []int
	for _, ln := range strings.Split(string(raw), "\n") {
		ln = strings.TrimSpace(ln)
		if ln == "" {
			continue
		}
		n, err := strconv.Atoi(ln)
		if err != nil {
			continue
		}
		pids = append(pids, n)
	}
	return pids
}

// FallbackLabelForScope returns the pricing_source suffix label.
//
//   - container → "" (full-fidelity)
//   - bare_metal_user_slice / root_cgroup → "no_container_scope"
//   - cgroup_v1 / unknown → "self_pid_only"
func FallbackLabelForScope(scope CgroupScope) string {
	switch scope.Kind {
	case CgroupKindContainer:
		return ""
	case CgroupKindBareMetalUserSlice, CgroupKindRootCgroup:
		return "no_container_scope"
	}
	return "self_pid_only"
}
