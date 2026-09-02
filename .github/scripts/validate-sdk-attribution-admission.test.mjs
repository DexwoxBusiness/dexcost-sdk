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

test("admits the current Python and TypeScript observer contract", () => {
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

test("rejects an invalid request predicate", () => {
  const candidate = inputs();
  const observer = candidate.manifest.observers.find(
    (entry) => entry.service_key === "exa_search",
  );
  observer.request_all[0].operator = "assume_default";

  expectIssue(
    validateSdkAttributionAdmission(candidate),
    "observer exa_search has invalid request predicates",
  );
});

test("rejects an empty not-equals request predicate value", () => {
  const candidate = inputs();
  const observer = candidate.manifest.observers.find(
    (entry) => entry.service_key === "aws_translate",
  );
  observer.request_all[0].value = "";

  expectIssue(
    validateSdkAttributionAdmission(candidate),
    "observer aws_translate has invalid request predicates",
  );
});

test("rejects malformed exact query predicates", () => {
  const candidate = inputs();
  const observer = candidate.manifest.observers.find(
    (entry) => entry.service_key === "azure_translator",
  );
  delete observer.query_all[0].value;

  expectIssue(
    validateSdkAttributionAdmission(candidate),
    "observer azure_translator has invalid query_all predicates",
  );
});

test("rejects unsafe dynamic provider-domain suffixes", () => {
  const candidate = inputs();
  const observer = candidate.manifest.observers.find(
    (entry) => entry.service_key === "azure_translator",
  );
  observer.domain_suffixes = ["com"];

  expectIssue(
    validateSdkAttributionAdmission(candidate),
    "observer azure_translator has invalid domain suffixes",
  );
});

test("requires a non-empty predicate for query-count multipliers", () => {
  const candidate = inputs();
  const observer = candidate.manifest.observers.find(
    (entry) => entry.service_key === "azure_translator",
  );
  observer.quantity_multiplier_query_parameter_count = "target";

  expectIssue(
    validateSdkAttributionAdmission(candidate),
    "observer azure_translator query-count multiplier lacks a non-empty predicate",
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

test("rejects a fixed-quantity fail-open case outside its endpoint boundary", () => {
  const candidate = inputs();
  const failOpen = candidate.conformance.cases.find(
    (entry) => entry.name === "brave_web_search_error_response_fails_open",
  );
  failOpen.url =
    "https://api.search.brave.com/res/v1/answers/search?q=dexcost";

  expectIssue(
    validateSdkAttributionAdmission(candidate),
    "case brave_web_search_error_response_fails_open does not target observer brave_search",
  );
});

test("rejects packaged observer drift in either paired SDK", () => {
  const candidate = inputs();
  candidate.packagedManifests.python.observers[0].usage_metric = "characters";

  expectIssue(
    validateSdkAttributionAdmission(candidate),
    "python packaged observer manifest differs from canonical",
  );
});

test("requires both paired shared conformance consumers", () => {
  const candidate = inputs();
  candidate.consumerTests.typescript = "";

  expectIssue(
    validateSdkAttributionAdmission(candidate),
    "typescript lacks the shared observer conformance consumer",
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
    "tests/service-usage-observer-conformance.test.ts",
    "tests/removed-observer-consumer.test.ts",
  );

  expectIssue(
    validateSdkAttributionAdmission(candidate),
    "CI does not execute the typescript observer conformance consumer",
  );
});

test("rejects CI that drops a paired SDK from the cross-SDK matrix", () => {
  const candidate = inputs();
  candidate.workflowText = candidate.workflowText.replace(
    "sdk: [python, typescript]",
    "sdk: [python]",
  );

  expectIssue(
    validateSdkAttributionAdmission(candidate),
    "CI cross-SDK matrix must include paired python and typescript SDKs",
  );
});
