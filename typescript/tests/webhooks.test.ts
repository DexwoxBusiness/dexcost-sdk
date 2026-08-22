import { createHmac } from "node:crypto";
import { describe, expect, it } from "vitest";
import {
  WebhookVerificationError, assertWebhookSignature, verifyWebhookSignature,
} from "../src/webhooks.js";

const NOW = 1_700_000_000;
const PAYLOAD = Buffer.from('{"event":"statement.generated","id":"evt_42"}');
const v1 = (secret: string, timestamp = String(NOW), payload = PAYLOAD): string =>
  `v1=${createHmac("sha256", secret).update(Buffer.concat([Buffer.from(timestamp), Buffer.from("."), payload])).digest("hex")}`;
const legacy = (secret: string): string => createHmac("sha256", secret).update(PAYLOAD).digest("hex");

describe("webhook verification", () => {
  it("verifies fresh raw-body signatures and rotated secrets", () => {
    expect(verifyWebhookSignature(PAYLOAD, v1("secret"), {
      timestampHeader: String(NOW), secrets: "secret", now: NOW,
    })).toBe(true);
    expect(verifyWebhookSignature(PAYLOAD, [v1("wrong"), `unrelated=abc, ${v1("new-secret")}`], {
      timestampHeader: String(NOW), secrets: ["old-secret", "new-secret"], now: NOW,
    })).toBe(true);
  });

  it.each([-301, 301])("rejects stale or future timestamp offset %s", (offset) => {
    const timestamp = String(NOW + offset);
    expect(verifyWebhookSignature(PAYLOAD, v1("secret", timestamp), {
      timestampHeader: timestamp, secrets: "secret", now: NOW,
    })).toBe(false);
  });

  it("rejects tampering and malformed adversarial input without throwing", () => {
    expect(verifyWebhookSignature(Buffer.concat([PAYLOAD, Buffer.from(" ")]), v1("secret"), {
      timestampHeader: String(NOW), secrets: "secret", now: NOW,
    })).toBe(false);
    expect(verifyWebhookSignature(PAYLOAD, "v1=xyz", {
      timestampHeader: String(NOW), secrets: "secret", now: NOW,
    })).toBe(false);
    expect(verifyWebhookSignature(PAYLOAD, Array(17).fill(v1("secret")), {
      timestampHeader: String(NOW), secrets: "secret", now: NOW,
    })).toBe(false);
  });

  it("requires explicit legacy opt-in and exposes one non-oracular assertion error", () => {
    expect(verifyWebhookSignature(PAYLOAD, legacy("secret"), { secrets: "secret" })).toBe(false);
    expect(verifyWebhookSignature(PAYLOAD, `sha256=${legacy("secret")}`, {
      secrets: "secret", allowLegacy: true,
    })).toBe(true);
    expect(() => assertWebhookSignature(PAYLOAD, v1("wrong"), {
      timestampHeader: String(NOW), secrets: "secret", now: NOW,
    })).toThrow(WebhookVerificationError);
  });
});
