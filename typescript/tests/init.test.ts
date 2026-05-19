import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { mkdtempSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { init, track, flush, close, getTracker } from "../src/index.js";

let tmpDir: string;

beforeEach(() => {
  tmpDir = mkdtempSync(join(tmpdir(), "dexcost-test-"));
});

describe("init() singleton", () => {
  afterEach(() => {
    try { close(); } catch { /* ignore if not initialized */ }
    rmSync(tmpDir, { recursive: true, force: true });
  });

  it("creates a global tracker on init()", () => {
    const tracker = init({ dbPath: join(tmpDir, "test.db") });
    expect(tracker).toBeDefined();
    expect(getTracker()).toBe(tracker);
  });

  it("throws on double init()", () => {
    init({ dbPath: join(tmpDir, "test.db") });
    expect(() => init({ dbPath: join(tmpDir, "test.db") })).toThrow("already initialized");
  });

  it("convenience track() delegates to singleton", async () => {
    init({ dbPath: join(tmpDir, "test.db") });
    await track({ taskType: "test" }, async (task) => {
      expect(task.task.taskType).toBe("test");
    });
  });

  it("convenience flush() delegates to singleton", async () => {
    init({ dbPath: join(tmpDir, "test.db") });
    await flush();
  });

  it("close() resets singleton so init() can be called again", () => {
    init({ dbPath: join(tmpDir, "test.db") });
    close();
    const tracker2 = init({ dbPath: join(tmpDir, "test.db") });
    expect(tracker2).toBeDefined();
  });
});
