/**
 * Tests for PII redaction and metadata safety utilities.
 */

import { describe, it, expect } from "vitest";
import {
  redactDict,
  hashValue,
  enforceMetadataLimit,
} from "../src/security/redaction.js";
import { eventToDict } from "../src/core/models.js";
import { createCostEvent } from "../src/core/models.js";
import { randomUUID } from "node:crypto";

describe("redactDict", () => {
  it("deletes specified fields entirely", () => {
    const data = {
      name: "John Doe",
      email: "john@example.com",
      age: 30,
      ssn: "123-45-6789",
    };

    const result = redactDict(data, ["email", "ssn"]);

    expect(result.name).toBe("John Doe");
    expect(result).not.toHaveProperty("email");
    expect(result.age).toBe(30);
    expect(result).not.toHaveProperty("ssn");
  });

  it("recursively deletes matched keys in nested objects", () => {
    const data = {
      user: {
        name: "Jane",
        email: "jane@example.com",
      },
      public: true,
    };

    const result = redactDict(data, ["email"]);

    expect(result.public).toBe(true);
    const user = result.user as Record<string, unknown>;
    expect(user.name).toBe("Jane");
    expect(user).not.toHaveProperty("email");
  });

  it("does not modify the original object", () => {
    const data = { secret: "password123", keep: "ok" };
    const result = redactDict(data, ["secret"]);

    expect(data.secret).toBe("password123");
    expect(result).not.toHaveProperty("secret");
    expect(result.keep).toBe("ok");
  });
});

describe("hashValue", () => {
  it("produces a SHA-256 hex digest", () => {
    const hash = hashValue("hello");

    // SHA-256 of "hello" is well-known
    expect(hash).toBe(
      "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    );
    expect(hash).toHaveLength(64); // SHA-256 hex is 64 chars
  });

  it("produces different hashes for different inputs", () => {
    const hash1 = hashValue("customer-1");
    const hash2 = hashValue("customer-2");

    expect(hash1).not.toBe(hash2);
  });

  it("produces consistent hashes for the same input", () => {
    const hash1 = hashValue("test-value");
    const hash2 = hashValue("test-value");

    expect(hash1).toBe(hash2);
  });
});

describe("enforceMetadataLimit", () => {
  it("returns the same data when under the limit", () => {
    const data = { key: "value", num: 42 };
    const result = enforceMetadataLimit(data, 1000);

    expect(result).toEqual(data);
  });

  it("returns a deterministic stub when data exceeds the limit", () => {
    // Create a large object
    const data: Record<string, unknown> = {};
    for (let i = 0; i < 100; i++) {
      data[`key_${i}`] = "x".repeat(200);
    }
    const originalSize = Buffer.byteLength(JSON.stringify(data), "utf-8");

    // ~100 keys * ~210 chars each = ~21KB, limit to 10KB
    const result = enforceMetadataLimit(data, 10_240);

    expect(result).toEqual({
      _truncated: true,
      _original_size_bytes: originalSize,
    });
  });

  it("returns a truncation stub for very small limit", () => {
    const data = { key: "a very long value that exceeds any small limit" };
    const result = enforceMetadataLimit(data, 5);

    expect(result._truncated).toBe(true);
    expect(typeof result._original_size_bytes).toBe("number");
  });

  it("returns an unserializable stub for circular data", () => {
    const data: Record<string, unknown> = {};
    data.self = data;
    const result = enforceMetadataLimit(data, 10_240);

    expect(result).toEqual({ _truncated: true, _error: "unserializable" });
  });

  it("uses 10KB default limit", () => {
    // Small data should pass through
    const small = { ok: true };
    expect(enforceMetadataLimit(small)).toEqual(small);
  });
});

describe("Event schema format", () => {
  it("serialization matches v1 schema field names", () => {
    const event = createCostEvent({
      eventId: randomUUID(),
      taskId: randomUUID(),
      eventType: "llm_call",
      costUsd: 0.05,
      costConfidence: "exact",
      provider: "openai",
      model: "gpt-4o",
      inputTokens: 800,
      outputTokens: 150,
      isRetry: false,
    });

    const dict = eventToDict(event);

    // Verify snake_case field names matching Python SDK
    expect(dict).toHaveProperty("event_id");
    expect(dict).toHaveProperty("task_id");
    expect(dict).toHaveProperty("event_type", "llm_call");
    expect(dict).toHaveProperty("occurred_at");
    expect(dict).toHaveProperty("cost_usd", "0.05");
    expect(dict).toHaveProperty("cost_confidence", "exact");
    expect(dict).toHaveProperty("provider", "openai");
    expect(dict).toHaveProperty("model", "gpt-4o");
    expect(dict).toHaveProperty("input_tokens", 800);
    expect(dict).toHaveProperty("output_tokens", 150);
    expect(dict).toHaveProperty("is_retry", false);
    expect(dict).toHaveProperty("schema_version", "1");
    expect(dict).toHaveProperty("details");

    // Verify cost is serialised as string (matching Python SDK behavior)
    expect(typeof dict.cost_usd).toBe("string");
  });
});
