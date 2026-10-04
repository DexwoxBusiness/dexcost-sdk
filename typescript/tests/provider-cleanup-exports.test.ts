import { describe, expect, it } from "vitest";
import * as dexcost from "../src/index.js";

describe("paired provider cleanup exports", () => {
  it.each([
    ["instrumentTextract", "uninstrumentTextract"],
    ["instrumentDocumentAI", "uninstrumentDocumentAI"],
    ["instrumentPinecone", "uninstrumentPinecone"],
    ["instrumentTurbopuffer", "uninstrumentTurbopuffer"],
  ] as const)("exports matching cleanup for %s", (instrument, cleanup) => {
    expect(typeof dexcost[instrument]).toBe("function");
    expect(typeof dexcost[cleanup]).toBe("function");
    // Cleanup remains harmless for an uninstrumented client and repeated calls.
    const client = {};
    expect(dexcost[cleanup](client)).toBeUndefined();
    expect(dexcost[cleanup](client)).toBeUndefined();
  });

  it("keeps category-level cleanup helpers internal", () => {
    expect(dexcost).not.toHaveProperty("uninstrumentOcr");
    expect(dexcost).not.toHaveProperty("uninstrumentVectorDatabase");
  });
});
