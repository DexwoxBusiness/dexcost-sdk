/**
 * Cgroup-scope classifier — Decision #1 of Phase 2 GPU foundation.
 * Mirrors python/tests/test_cgroup_walker.py.
 */

import { describe, it, expect, beforeEach, afterEach } from "vitest";
import {
  mkdtempSync,
  mkdirSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import {
  _setCgroupRootForTests,
  _setProcSelfCgroupPathForTests,
  _resetWarningStateForTests,
  classifyScope,
  enumeratePids,
  fallbackLabelFor,
  type CgroupScope,
} from "../src/core/cgroup-walker.js";

function withTmpdir(): { dir: string; setProc: (s: string) => void; cleanup: () => void } {
  const dir = mkdtempSync(join(tmpdir(), "dexcost-cgroup-walker-"));
  const procPath = join(dir, "cgroup");
  _setProcSelfCgroupPathForTests(procPath);
  return {
    dir,
    setProc: (s: string) => writeFileSync(procPath, s),
    cleanup: () => rmSync(dir, { recursive: true, force: true }),
  };
}

describe("classifyScope — Decision #1 table", () => {
  let tmp: ReturnType<typeof withTmpdir>;
  beforeEach(() => {
    _resetWarningStateForTests();
    tmp = withTmpdir();
  });
  afterEach(() => {
    tmp.cleanup();
    _setProcSelfCgroupPathForTests(null);
    _setCgroupRootForTests(null);
  });

  const table: [string, string, string][] = [
    ["docker", "0::/docker/abc123\n", "container"],
    ["kubepods.slice", "0::/kubepods.slice/kubepods-burstable.slice/foo.scope\n", "container"],
    ["kubepods (legacy)", "0::/kubepods/burstable/podabc/abc\n", "container"],
    ["system.slice docker-", "0::/system.slice/docker-abc.scope\n", "container"],
    ["system.slice containerd-", "0::/system.slice/containerd-abc.scope\n", "container"],
    ["system.slice crio-", "0::/system.slice/crio-abc.scope\n", "container"],
    ["containerd", "0::/containerd/abc\n", "container"],
    ["crio", "0::/crio/abc\n", "container"],
    ["user.slice", "0::/user.slice/user-1000.slice/session-2.scope\n", "bare_metal_user_slice"],
    [
      "user.slice deep",
      "0::/user.slice/user-1000.slice/user@1000.service/app.slice/unit.service\n",
      "bare_metal_user_slice",
    ],
    ["root", "0::/\n", "root_cgroup"],
    ["unknown prefix", "0::/some/unknown/path\n", "unknown"],
  ];

  it.each(table)("%s → %s", (_name, content, expected) => {
    tmp.setProc(content);
    expect(classifyScope().kind).toBe(expected);
  });

  it("multi-line file → cgroup_v1", () => {
    tmp.setProc(
      "12:devices:/docker/abc\n11:cpuset:/docker/abc\n10:memory:/docker/abc\n",
    );
    expect(classifyScope().kind).toBe("cgroup_v1");
  });

  it("missing /proc/self/cgroup → unknown", () => {
    _setProcSelfCgroupPathForTests("/nonexistent/path/cgroup");
    const scope = classifyScope();
    expect(scope.kind).toBe("unknown");
    expect(scope.path).toBe(null);
  });

  it("container scope carries the resolved path", () => {
    tmp.setProc("0::/kubepods.slice/kubepods-burstable.slice/foo.scope\n");
    const scope = classifyScope();
    expect(scope.path).toBe(
      "/kubepods.slice/kubepods-burstable.slice/foo.scope",
    );
  });
});

describe("enumeratePids — container walks; non-container returns self-PID only", () => {
  let tmp: ReturnType<typeof withTmpdir>;
  beforeEach(() => {
    _resetWarningStateForTests();
    tmp = withTmpdir();
  });
  afterEach(() => {
    tmp.cleanup();
    _setProcSelfCgroupPathForTests(null);
    _setCgroupRootForTests(null);
  });

  it("container scope walks /sys/fs/cgroup/<path>/cgroup.procs", () => {
    tmp.setProc("0::/docker/abc123\n");
    const root = join(tmp.dir, "fake_cgroup_root");
    mkdirSync(join(root, "docker", "abc123"), { recursive: true });
    writeFileSync(
      join(root, "docker", "abc123", "cgroup.procs"),
      "1234\n5678\n9012\n",
    );
    _setCgroupRootForTests(root);

    const scope = classifyScope();
    const pids = enumeratePids(scope);
    expect(pids).toEqual([1234, 5678, 9012]);
  });

  it("bare_metal_user_slice → [self_pid] (silent-overcount guard)", () => {
    tmp.setProc("0::/user.slice/user-1000.slice/session-2.scope\n");
    const scope = classifyScope();
    expect(enumeratePids(scope)).toEqual([process.pid]);
  });

  it("root_cgroup → [self_pid] (ambiguous)", () => {
    tmp.setProc("0::/\n");
    expect(enumeratePids(classifyScope())).toEqual([process.pid]);
  });

  it("unknown scope → [self_pid]", () => {
    tmp.setProc("0::/some/unknown/path\n");
    expect(enumeratePids(classifyScope())).toEqual([process.pid]);
  });

  it("cgroup_v1 → [self_pid] (v1.1 deferred)", () => {
    tmp.setProc("12:devices:/docker/abc\n11:cpuset:/docker/abc\n");
    expect(enumeratePids(classifyScope())).toEqual([process.pid]);
  });

  it("container walk denied → null + warn-once", () => {
    tmp.setProc("0::/docker/abc123\n");
    _setCgroupRootForTests(join(tmp.dir, "nonexistent"));
    const scope = classifyScope();
    expect(enumeratePids(scope)).toBeNull();
  });
});

describe("fallbackLabelFor — Decision #1 pricing-source suffix", () => {
  it("container → null (no fallback)", () => {
    const s: CgroupScope = { kind: "container", path: "/docker/abc" };
    expect(fallbackLabelFor(s)).toBeNull();
  });
  it("bare_metal_user_slice → no_container_scope", () => {
    expect(fallbackLabelFor({ kind: "bare_metal_user_slice", path: null })).toBe(
      "no_container_scope",
    );
  });
  it("root_cgroup → no_container_scope", () => {
    expect(fallbackLabelFor({ kind: "root_cgroup", path: null })).toBe(
      "no_container_scope",
    );
  });
  it("unknown → self_pid_only", () => {
    expect(fallbackLabelFor({ kind: "unknown", path: null })).toBe(
      "self_pid_only",
    );
  });
  it("cgroup_v1 → self_pid_only", () => {
    expect(fallbackLabelFor({ kind: "cgroup_v1", path: null })).toBe(
      "self_pid_only",
    );
  });
});
