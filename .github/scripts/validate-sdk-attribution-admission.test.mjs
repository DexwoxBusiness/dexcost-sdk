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

test("rejects a non-scalar equals request predicate", () => {
  const candidate = inputs();
  const observer = candidate.manifest.observers.find(
    (entry) => entry.service_key === "deepl_translate_billed_characters",
  );
  observer.request_all[0].value = { assumed: true };

  expectIssue(
    validateSdkAttributionAdmission(candidate),
    "observer deepl_translate_billed_characters has invalid request predicates",
  );
});

test("requires positive finite response collection sums", () => {
  const candidate = inputs();
  const testCase = candidate.conformance.cases.find(
    (entry) =>
      entry.name === "deepl_uses_provider_reported_billed_characters_when_requested",
  );
  testCase.response.translations[0].billed_characters = -1;

  expectIssue(
    validateSdkAttributionAdmission(candidate),
    "case deepl_uses_provider_reported_billed_characters_when_requested does not target observer deepl_translate_billed_characters",
  );
});

test("rejects request-header predicates that could retain noncanonical names", () => {
  const candidate = inputs();
  const observer = candidate.manifest.observers.find(
    (entry) => entry.service_key === "jina_reader",
  );
  observer.request_header_all[0].name = "Authorization";

  expectIssue(
    validateSdkAttributionAdmission(candidate),
    "observer jina_reader has invalid request-header predicates",
  );
});

test("rejects malformed value-aware request-header predicates", () => {
  const candidate = inputs();
  const observer = candidate.manifest.observers.find(
    (entry) => entry.service_key === "aws_rekognition_image_group_1",
  );
  observer.request_header_all[0].values.push(observer.request_header_all[0].values[0]);

  expectIssue(
    validateSdkAttributionAdmission(candidate),
    "observer aws_rekognition_image_group_1 has invalid request-header predicates",
  );
});

test("matches request-header predicates by value, not presence alone", () => {
  const candidate = inputs();
  const testCase = candidate.conformance.cases.find(
    (entry) =>
      entry.name === "aws_rekognition_group_1_request_captures_region_and_request_id",
  );
  testCase.request_headers["x-amz-target"] = "RekognitionService.StartLabelDetection";

  expectIssue(
    validateSdkAttributionAdmission(candidate),
    "case aws_rekognition_group_1_request_captures_region_and_request_id does not target observer aws_rekognition_image_group_1",
  );
});

test("requires paired provider-region extraction fields", () => {
  const candidate = inputs();
  const observer = candidate.manifest.observers.find(
    (entry) => entry.service_key === "aws_rekognition_image_group_1",
  );
  delete observer.allowed_provider_regions;

  expectIssue(
    validateSdkAttributionAdmission(candidate),
    "observer aws_rekognition_image_group_1 has invalid provider-region extraction",
  );
});

test("rejects invalid request-collection predicates", () => {
  const candidate = inputs();
  const observer = candidate.manifest.observers.find(
    (entry) => entry.service_key === "google_vision_label",
  );
  observer.request_collection_all[0].operator = "assume_contains";

  expectIssue(
    validateSdkAttributionAdmission(candidate),
    "observer google_vision_label has invalid request-collection predicates",
  );
});

test("requires request-collection paths and predicates together", () => {
  const candidate = inputs();
  const observer = candidate.manifest.observers.find(
    (entry) => entry.service_key === "google_vision_label",
  );
  delete observer.request_collection_all;

  expectIssue(
    validateSdkAttributionAdmission(candidate),
    "observer google_vision_label must pair collection count and predicates",
  );
});

test("requires paired response predicates to be fail-open compatible", () => {
  const candidate = inputs();
  const observer = candidate.manifest.observers.find(
    (entry) => entry.service_key === "google_vision_label",
  );
  observer.paired_response_all[0].operator = "present";

  expectIssue(
    validateSdkAttributionAdmission(candidate),
    "observer google_vision_label has invalid paired-response predicates",
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

test("rejects an empty string-not-contains request predicate value", () => {
  const candidate = inputs();
  const observer = candidate.manifest.observers.find(
    (entry) => entry.service_key === "resemble_ai",
  );
  observer.request_all[0].value = "";

  expectIssue(
    validateSdkAttributionAdmission(candidate),
    "observer resemble_ai has invalid request predicates",
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

test("rejects malformed endpoint exclusions", () => {
  const candidate = inputs();
  const observer = candidate.manifest.observers.find(
    (entry) => entry.service_key === "github_api",
  );
  observer.excluded_endpoints = ["graphql"];

  expectIssue(
    validateSdkAttributionAdmission(candidate),
    "observer github_api has invalid excluded endpoints",
  );
});

test("does not accept an exact-route descendant as positive coverage", () => {
  const candidate = inputs();
  const entry = candidate.admission.observers.find(
    (item) => item.service_key === "fireworks_embeddings",
  );
  const testCase = candidate.conformance.cases.find(
    (item) => item.name === entry.positive_cases[0],
  );
  testCase.url += "/preview";

  expectIssue(
    validateSdkAttributionAdmission(candidate),
    `case ${testCase.name} does not target observer fireworks_embeddings`,
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
