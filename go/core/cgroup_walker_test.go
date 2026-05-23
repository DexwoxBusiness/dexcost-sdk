// Task 2 — cgroup-scope classifier tests. Mirrors python commit caebcf7.
//
// The classifier is the Decision #1 verification-gate boundary: it inspects
// /proc/self/cgroup, identifies the scope, and tells GpuAccountant whether
// to walk the cgroup's PIDs or degrade to self-PID-only.

package core

import (
	"os"
	"path/filepath"
	"strconv"
	"testing"
)

// writeCgroupFile creates a fake /proc/self/cgroup-style file.
func writeCgroupFile(t *testing.T, content string) string {
	t.Helper()
	dir := t.TempDir()
	path := filepath.Join(dir, "cgroup")
	if err := os.WriteFile(path, []byte(content), 0644); err != nil {
		t.Fatalf("write fake cgroup file: %v", err)
	}
	return path
}

func TestClassifyScopeContainerKubepodsSlice(t *testing.T) {
	procPath := writeCgroupFile(t,
		"0::/kubepods.slice/kubepods-burstable.slice/kubepods-burstable-pod1234.slice/cri-containerd-abcd.scope\n")
	scope := classifyCgroupScopeFromFile(procPath)
	if scope.Kind != CgroupKindContainer {
		t.Fatalf("kubepods.slice should classify as container; got %q", scope.Kind)
	}
	if scope.Path == "" {
		t.Fatalf("container scope should have a non-empty Path")
	}
}

func TestClassifyScopeContainerPrefixes(t *testing.T) {
	// All seven container-prefix variants from Decision #1's table.
	cases := []struct{ input string }{
		{"0::/kubepods.slice/kubepods-pod1.slice\n"},
		{"0::/kubepods/burstable/pod1\n"},
		{"0::/docker/abc123\n"},
		{"0::/system.slice/docker-abc.scope\n"},
		{"0::/containerd/abc\n"},
		{"0::/system.slice/containerd-abc.scope\n"},
		{"0::/crio/abc\n"},
		{"0::/system.slice/crio-abc.scope\n"},
	}
	for _, c := range cases {
		path := writeCgroupFile(t, c.input)
		scope := classifyCgroupScopeFromFile(path)
		if scope.Kind != CgroupKindContainer {
			t.Errorf("%q should classify as container; got %q", c.input, scope.Kind)
		}
	}
}

func TestClassifyScopeBareMetalUserSlice(t *testing.T) {
	procPath := writeCgroupFile(t,
		"0::/user.slice/user-1000.slice/session-1.scope\n")
	scope := classifyCgroupScopeFromFile(procPath)
	if scope.Kind != CgroupKindBareMetalUserSlice {
		t.Fatalf("user.slice should classify as bare_metal_user_slice; got %q", scope.Kind)
	}
	if scope.Path != "" {
		t.Fatalf("bare_metal scope should have empty Path; got %q", scope.Path)
	}
}

func TestClassifyScopeRootCgroup(t *testing.T) {
	procPath := writeCgroupFile(t, "0::/\n")
	scope := classifyCgroupScopeFromFile(procPath)
	if scope.Kind != CgroupKindRootCgroup {
		t.Fatalf("root cgroup should classify as root_cgroup; got %q", scope.Kind)
	}
}

func TestClassifyScopeCgroupV1MultiLine(t *testing.T) {
	procPath := writeCgroupFile(t,
		"12:devices:/docker/abc\n"+
			"11:cpu,cpuacct:/docker/abc\n"+
			"10:memory:/docker/abc\n")
	scope := classifyCgroupScopeFromFile(procPath)
	if scope.Kind != CgroupKindCgroupV1 {
		t.Fatalf("multi-line should classify as cgroup_v1; got %q", scope.Kind)
	}
}

func TestClassifyScopeUnknown(t *testing.T) {
	procPath := writeCgroupFile(t, "0::/something/weird\n")
	scope := classifyCgroupScopeFromFile(procPath)
	if scope.Kind != CgroupKindUnknown {
		t.Fatalf("unmatched path should classify as unknown; got %q", scope.Kind)
	}
}

func TestClassifyScopeMissingFile(t *testing.T) {
	scope := classifyCgroupScopeFromFile("/no/such/file/anywhere")
	if scope.Kind != CgroupKindUnknown {
		t.Fatalf("missing file should fall back to unknown; got %q", scope.Kind)
	}
}

func TestEnumeratePIDsBareMetalReturnsSelfPID(t *testing.T) {
	scope := CgroupScope{Kind: CgroupKindBareMetalUserSlice}
	pids := EnumerateCgroupPIDs(scope, "")
	if len(pids) != 1 || pids[0] != os.Getpid() {
		t.Fatalf("bare-metal should degrade to self-PID-only; got %v", pids)
	}
}

func TestEnumeratePIDsRootCgroupReturnsSelfPID(t *testing.T) {
	scope := CgroupScope{Kind: CgroupKindRootCgroup}
	pids := EnumerateCgroupPIDs(scope, "")
	if len(pids) != 1 || pids[0] != os.Getpid() {
		t.Fatalf("root cgroup should degrade to self-PID-only; got %v", pids)
	}
}

func TestEnumeratePIDsCgroupV1ReturnsSelfPID(t *testing.T) {
	scope := CgroupScope{Kind: CgroupKindCgroupV1}
	pids := EnumerateCgroupPIDs(scope, "")
	if len(pids) != 1 || pids[0] != os.Getpid() {
		t.Fatalf("cgroup_v1 should degrade to self-PID-only; got %v", pids)
	}
}

func TestEnumeratePIDsContainerWalksProcsFile(t *testing.T) {
	// Fake cgroup root with a cgroup.procs file inside.
	root := t.TempDir()
	containerPath := "/kubepods.slice/kubepods-pod1.slice"
	dir := filepath.Join(root, containerPath)
	if err := os.MkdirAll(dir, 0755); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	procsFile := filepath.Join(dir, "cgroup.procs")
	content := "1234\n5678\n9012\n"
	if err := os.WriteFile(procsFile, []byte(content), 0644); err != nil {
		t.Fatalf("write procs: %v", err)
	}

	scope := CgroupScope{Kind: CgroupKindContainer, Path: containerPath}
	pids := EnumerateCgroupPIDs(scope, root)
	want := []int{1234, 5678, 9012}
	if len(pids) != len(want) {
		t.Fatalf("PIDs len: got %v; want %v", pids, want)
	}
	for i, w := range want {
		if pids[i] != w {
			t.Fatalf("PID[%d] = %d; want %d", i, pids[i], w)
		}
	}
}

func TestEnumeratePIDsContainerReadFailureReturnsNil(t *testing.T) {
	// container scope but no cgroup.procs at path → nil (caller degrades).
	scope := CgroupScope{Kind: CgroupKindContainer, Path: "/kubepods.slice/nope"}
	pids := EnumerateCgroupPIDs(scope, t.TempDir())
	if pids != nil {
		t.Fatalf("expected nil when cgroup.procs unreadable; got %v", pids)
	}
}

// ─── Decision #1 fallback labels ───────────────────────────────────────

func TestFallbackLabelContainerReturnsEmpty(t *testing.T) {
	if l := FallbackLabelForScope(CgroupScope{Kind: CgroupKindContainer}); l != "" {
		t.Fatalf("container should have no fallback label; got %q", l)
	}
}

func TestFallbackLabelBareMetalReturnsNoContainerScope(t *testing.T) {
	if l := FallbackLabelForScope(CgroupScope{Kind: CgroupKindBareMetalUserSlice}); l != "no_container_scope" {
		t.Fatalf("bare_metal label = %q; want no_container_scope", l)
	}
	if l := FallbackLabelForScope(CgroupScope{Kind: CgroupKindRootCgroup}); l != "no_container_scope" {
		t.Fatalf("root_cgroup label = %q; want no_container_scope", l)
	}
}

func TestFallbackLabelCgroupV1Unknown(t *testing.T) {
	if l := FallbackLabelForScope(CgroupScope{Kind: CgroupKindCgroupV1}); l != "self_pid_only" {
		t.Fatalf("cgroup_v1 label = %q; want self_pid_only", l)
	}
	if l := FallbackLabelForScope(CgroupScope{Kind: CgroupKindUnknown}); l != "self_pid_only" {
		t.Fatalf("unknown label = %q; want self_pid_only", l)
	}
}

// Sanity: prefix table is exhaustive (no missing common Kubernetes shape).
func TestCgroupPrefixTableIsComplete(t *testing.T) {
	expectedPrefixes := []string{
		"/kubepods.slice/", "/kubepods/", "/docker/",
		"/system.slice/docker-", "/containerd/",
		"/system.slice/containerd-", "/crio/",
		"/system.slice/crio-",
	}
	for _, p := range expectedPrefixes {
		found := false
		for _, actual := range containerPrefixes {
			if actual == p {
				found = true
				break
			}
		}
		if !found {
			t.Errorf("prefix table missing %q (Decision #1)", p)
		}
	}
}

// Don't trip ourselves by silently swallowing non-int lines in cgroup.procs.
func TestEnumeratePIDsContainerIgnoresMalformedLines(t *testing.T) {
	root := t.TempDir()
	containerPath := "/docker/abc"
	dir := filepath.Join(root, containerPath)
	_ = os.MkdirAll(dir, 0755)
	_ = os.WriteFile(filepath.Join(dir, "cgroup.procs"),
		[]byte("1234\nnot-a-pid\n5678\n\n"), 0644)

	scope := CgroupScope{Kind: CgroupKindContainer, Path: containerPath}
	pids := EnumerateCgroupPIDs(scope, root)
	if len(pids) != 2 {
		t.Fatalf("expected 2 valid PIDs; got %v", pids)
	}
	if strconv.Itoa(pids[0]) != "1234" || strconv.Itoa(pids[1]) != "5678" {
		t.Fatalf("PIDs unexpected: %v", pids)
	}
}
