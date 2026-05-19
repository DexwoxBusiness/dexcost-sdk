import { describe, it, expect } from "vitest";
import { mkdtempSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { EventBuffer } from "../src/transport/buffer.js";
import { ServiceCatalog } from "../src/pricing/service-catalog.js";
import { PricingEngine } from "../src/pricing/engine.js";

describe("Error handling", () => {
  describe("EventBuffer", () => {
    it("handles corrupt JSON in event details without crashing", () => {
      const dir = mkdtempSync(join(tmpdir(), "dexcost-test-"));
      const dbPath = join(dir, "test.db");
      const buffer = new EventBuffer(dbPath);

      // Directly insert a row with corrupt JSON
      const db = (buffer as any)._db;
      db.prepare(`INSERT INTO events (event_id, task_id, event_type, cost_usd, timestamp, sync_status, details)
        VALUES (?, ?, ?, ?, ?, ?, ?)`).run(
        "test-event-id", "test-task-id", "llm_call", "0.01",
        new Date().toISOString(), "pending", "{corrupt json"
      );

      // Should not crash when reading events
      const events = buffer.getAllEvents();
      expect(events.length).toBe(1);
      expect(events[0].details).toEqual({});

      buffer.close();
      rmSync(dir, { recursive: true, force: true });
    });
  });

  describe("ServiceCatalog", () => {
    it("handles missing catalog file gracefully", () => {
      const catalog = new ServiceCatalog("/nonexistent/path/catalog.json");
      expect(catalog.catalogVersion).toBeTruthy();
    });

    it("handles corrupt catalog file gracefully", () => {
      const dir = mkdtempSync(join(tmpdir(), "dexcost-test-"));
      const badFile = join(dir, "bad.json");
      writeFileSync(badFile, "{not valid json");

      const catalog = new ServiceCatalog(badFile);
      expect(catalog.catalogVersion).toBeTruthy();

      rmSync(dir, { recursive: true, force: true });
    });

    it("returns null for NaN cost extraction", () => {
      const catalog = new ServiceCatalog();
      const entry = catalog.lookup("https://app.scrapingbee.com/api/v1/");
      if (!entry) return; // Skip if not in catalog

      const result = catalog.extractCost(
        entry,
        new Headers({ "Spb-cost": "not_a_number" }),
        null,
      );
      expect(result).toBeNull();
    });
  });

  describe("PricingEngine", () => {
    it("returns unknown cost for unrecognized model", () => {
      const engine = new PricingEngine();
      const result = engine.getCost("nonexistent-model-xyz", 100, 50);
      expect(result.costConfidence).toBe("unknown");
      expect(result.costUsd).toBe(0);
    });
  });
});
