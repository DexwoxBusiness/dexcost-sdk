package core

import (
	"context"
	"os/exec"
	"strconv"
	"strings"
	"time"
)

// nvidiaSMIBackend keeps the Go SDK dependency-free while providing a real
// local NVIDIA capture path. Every probe is bounded and fail-open.
type nvidiaSMIBackend struct {
	path string
}

func newNvidiaSMIBackend() NVMLBackend {
	return &nvidiaSMIBackend{path: "nvidia-smi"}
}

func (b *nvidiaSMIBackend) run(args ...string) (string, bool) {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	out, err := exec.CommandContext(ctx, b.path, args...).Output()
	if err != nil || ctx.Err() != nil {
		return "", false
	}
	return string(out), true
}

func (b *nvidiaSMIBackend) Available() bool {
	_, ok := b.run("--query-gpu=count", "--format=csv,noheader")
	return ok
}

func (b *nvidiaSMIBackend) Init() bool { return b.Available() }
func (b *nvidiaSMIBackend) Shutdown()  {}

func firstNonEmptyLine(raw string) string {
	for _, line := range strings.Split(raw, "\n") {
		if trimmed := strings.TrimSpace(line); trimmed != "" {
			return trimmed
		}
	}
	return ""
}

func (b *nvidiaSMIBackend) DeviceCount() (int, bool) {
	out, ok := b.run("--query-gpu=count", "--format=csv,noheader")
	if !ok {
		return 0, false
	}
	n, err := strconv.Atoi(firstNonEmptyLine(out))
	return n, err == nil && n >= 0
}

func (b *nvidiaSMIBackend) ProductName(devIdx int) (string, bool) {
	out, ok := b.run(
		"--query-gpu=name", "--format=csv,noheader", "-i", strconv.Itoa(devIdx),
	)
	name := firstNonEmptyLine(out)
	return name, ok && name != ""
}

func (b *nvidiaSMIBackend) MIGMode(devIdx int) bool {
	out, ok := b.run(
		"--query-gpu=mig.mode.current", "--format=csv,noheader", "-i", strconv.Itoa(devIdx),
	)
	return ok && strings.EqualFold(firstNonEmptyLine(out), "Enabled")
}

func parseSmiInt(raw string) (int64, bool) {
	value := strings.TrimSpace(raw)
	if value == "" || strings.EqualFold(value, "N/A") || value == "-" {
		return 0, false
	}
	n, err := strconv.ParseInt(value, 10, 64)
	return n, err == nil
}

func (b *nvidiaSMIBackend) ComputeRunningProcesses(devIdx int) ([]NVMLProcessInfo, bool) {
	out, ok := b.run(
		"--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits",
		"-i", strconv.Itoa(devIdx),
	)
	if !ok {
		return nil, false
	}
	processes := make([]NVMLProcessInfo, 0)
	for _, line := range strings.Split(out, "\n") {
		parts := strings.Split(line, ",")
		if len(parts) < 2 {
			continue
		}
		pid, pidOK := parseSmiInt(parts[0])
		memoryMiB, memoryOK := parseSmiInt(parts[1])
		if !pidOK {
			continue
		}
		if !memoryOK {
			memoryMiB = 0
		}
		processes = append(processes, NVMLProcessInfo{
			PID: int(pid), UsedGPUMemory: memoryMiB * 1024 * 1024,
		})
	}
	return processes, true
}

func parsePmonUtilization(raw string, timestampUS int64) map[int][]NVMLUtilSample {
	result := map[int][]NVMLUtilSample{}
	for _, rawLine := range strings.Split(raw, "\n") {
		line := strings.TrimSpace(rawLine)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		// Without `-o T`, pmon columns are: gpu pid type sm mem ... command.
		parts := strings.Fields(line)
		if len(parts) < 5 {
			continue
		}
		pid, pidOK := parseSmiInt(parts[1])
		sm, smOK := parseSmiInt(parts[3])
		mem, memOK := parseSmiInt(parts[4])
		if !pidOK || !smOK || !memOK {
			continue
		}
		sample := NVMLUtilSample{
			PID: int(pid), SMUtil: int(sm), MemUtil: int(mem), TimeStamp: timestampUS,
		}
		result[int(pid)] = append(result[int(pid)], sample)
	}
	return result
}

func (b *nvidiaSMIBackend) ProcessUtilization(
	devIdx int,
	_ map[int]int64,
) (map[int][]NVMLUtilSample, bool) {
	out, ok := b.run("pmon", "-c", "1", "-s", "u", "-i", strconv.Itoa(devIdx))
	if !ok {
		return nil, false
	}
	return parsePmonUtilization(out, time.Now().UnixMicro()), true
}

func (b *nvidiaSMIBackend) MemoryInfo(devIdx int) (NVMLMemInfo, bool) {
	out, ok := b.run(
		"--query-gpu=memory.used,memory.total", "--format=csv,noheader,nounits",
		"-i", strconv.Itoa(devIdx),
	)
	parts := strings.Split(firstNonEmptyLine(out), ",")
	if !ok || len(parts) < 2 {
		return NVMLMemInfo{}, false
	}
	usedMiB, usedOK := parseSmiInt(parts[0])
	totalMiB, totalOK := parseSmiInt(parts[1])
	if !usedOK || !totalOK {
		return NVMLMemInfo{}, false
	}
	return NVMLMemInfo{
		UsedBytes: usedMiB * 1024 * 1024, TotalBytes: totalMiB * 1024 * 1024,
	}, true
}
