import assert from "node:assert/strict";
import { dirname, resolve } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  loadSdkAttributionAdmissionInputs,
  validateSdkAttributionAdmission,
} from "./validate-sdk-attribution-admission.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, "..", "..");
const validInputs = await loadSdkAttributionAdmissionInputs(root);

function inputs() {
  return structuredClone(validInputs);
}

function expectIssue(issues, expected) {
  assert.ok(
    issues.includes(expected),
    `expected issue ${JSON.stringify(expected)} in:\n${issues.join("\n")}`,
  );
}

test("admits the current four-SDK observer contract", () => {
  assert.deepEqual(validateSdkAttributionAdmission(inputs()), []);
});

test("rejects an enabled observer without an admission declaration", () => {
  const candidate = inputs();
  candidate.admission.observers = candidate.admission.observers.filter(
    (entry) => entry.service_key !== "cohere_embed",
  );

  expectIssue(
    validateSdkAttributionAdmission(candidate),
    "observer cohere_embed lacks an admission declaration",
  );
});

test("rejects monetary authority in the SDK observer manifest", () => {
  const candidate = inputs();
  candidate.manifest.observers[0].rate_amount = "0.000001";

  expectIssue(
    validateSdkAttributionAdmission(candidate),
    "SDK observer manifest asserts monetary authority at $.observers[0].rate_amount",
  );
});

test("rejects fixed request quantity with a non-request metric", () => {
  const candidate = inputs();
  const observer = candidate.manifest.observers.find(
    (entry) => entry.service_key === "brave_search",
  );
  observer.usage_metric = "input_tokens";

  expectIssue(
    validateSdkAttributionAdmission(candidate),
    "observer brave_search fixed_quantity and request_count must be declared together",
  );
});

test("rejects conformance dimensions that disagree with the observer", () => {
  const candidate = inputs();
  candidate.conformance.cases[0].expected[0].metric = "characters";

  expectIssue(
    validateSdkAttributionAdmission(candidate),
    "conformance case openai_embeddings_uses_total_tokens_and_request_id disagrees with openai_embeddings.usage_metric",
  );
});

test("rejects a fail-open case that emits positive usage", () => {
  const candidate = inputs();
  const positive = candidate.conformance.cases.find(
    (entry) => entry.name === "openai_embeddings_uses_total_tokens_and_request_id",
  );
  const failOpen = candidate.conformance.cases.find(
    (entry) => entry.name === "openai_embeddings_missing_usage_is_not_invented",
  );
  failOpen.expected = structuredClone(positive.expected);

  expectIssue(
    validateSdkAttributionAdmission(candidate),
    "fail-open case openai_embeddings_missing_usage_is_not_invented unexpectedly emits openai_embeddings",
  );
});

test("rejects packaged observer drift in any language", () => {
  const candidate = inputs();
  candidate.packagedManifests.go.observers[0].usage_metric = "characters";

  expectIssue(
    validateSdkAttributionAdmission(candidate),
    "go packaged observer manifest differs from canonical",
  );
});

test("requires all four shared conformance consumers", () => {
  const candidate = inputs();
  candidate.consumerTests.rust = "";

  expectIssue(
    validateSdkAttributionAdmission(candidate),
    "rust lacks the shared observer conformance consumer",
  );
});

test("rejects a non-provider observer source", () => {
  const candidate = inputs();
  candidate.manifest.observers[0].source_url = "https://example.com/api-reference";

  expectIssue(
    validateSdkAttributionAdmission(candidate),
    "observer openai_embeddings source is not provider-owned",
  );
});

test("does not treat a provider documentation mapping as a generic bypass", () => {
  const candidate = inputs();
  const observer = candidate.manifest.observers.find(
    (entry) => entry.service_key === "google_custom_search",
  );
  observer.domains = ["api.example.com"];

  expectIssue(
    validateSdkAttributionAdmission(candidate),
    "observer google_custom_search source is not provider-owned",
  );
});

test("rejects CI that stops executing a language consumer", () => {
  const candidate = inputs();
  candidate.workflowText = candidate.workflowText.replace(
    "cargo test --test service_usage_observer_conformance",
    "cargo test --test removed_observer_consumer",
  );

  expectIssue(
    validateSdkAttributionAdmission(candidate),
    "CI does not execute the rust observer conformance consumer",
  );
});
