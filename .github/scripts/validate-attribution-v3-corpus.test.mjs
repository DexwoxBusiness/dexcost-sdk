import assert from "node:assert/strict";
import { dirname, resolve } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  loadAttributionV3Corpus,
  validateAttributionV3Corpus,
} from "./validate-attribution-v3-corpus.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, "..", "..");
const validInputs = await loadAttributionV3Corpus(root);

function inputs() {
  return structuredClone(validInputs);
}

function expectIssue(issues, expected) {
  assert.ok(
    issues.includes(expected),
    `expected issue ${JSON.stringify(expected)} in:\n${issues.join("\n")}`,
  );
}

test("admits the locked attribution-v3 corpus", () => {
  assert.deepEqual(validateAttributionV3Corpus(inputs()), []);
});

test("rejects missing contract coverage", () => {
  const candidate = inputs();
  for (const testCase of candidate.corpus.valid_observations) {
    testCase.covers = testCase.covers.filter(
      (tag) => tag !== "observation.unknown_meter_visible",
    );
  }
  expectIssue(
    validateAttributionV3Corpus(candidate),
    "required coverage is missing: observation.unknown_meter_visible",
  );
});

test("rejects narrowing the locked guarantee inventory", () => {
  const candidate = inputs();
  candidate.manifest.required_coverage = candidate.manifest.required_coverage.filter(
    (tag) => tag !== "observation.unknown_meter_visible",
  );
  for (const testCase of candidate.corpus.valid_observations) {
    testCase.covers = testCase.covers.filter(
      (tag) => tag !== "observation.unknown_meter_visible",
    );
  }
  expectIssue(
    validateAttributionV3Corpus(candidate),
    "required_coverage does not match the locked v3 guarantee inventory",
  );
});

test("rejects duplicate case identities across streams", () => {
  const candidate = inputs();
  candidate.corpus.outcomes.valid[0].id = candidate.corpus.valid_observations[0].id;
  expectIssue(
    validateAttributionV3Corpus(candidate),
    `case id is duplicated: ${candidate.corpus.valid_observations[0].id}`,
  );
});

test("rejects an unresolvable mutation fixture", () => {
  const candidate = inputs();
  candidate.corpus.invalid_observations[1].mutate_from = "missing.base.case";
  expectIssue(
    validateAttributionV3Corpus(candidate),
    "observation.invalid.numeric_quantity references unknown mutate_from missing.base.case",
  );
});

test("rejects unstable line identity in a revision sequence", () => {
  const candidate = inputs();
  candidate.corpus.revision_sequences[0].revisions[2].usage[0].line_id =
    "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb9";
  expectIssue(
    validateAttributionV3Corpus(candidate),
    "revision.voice_full_snapshot changed line identity for metric connected_seconds",
  );
});

test("rejects SDK evidence that claims invoice authority", () => {
  const candidate = inputs();
  candidate.corpus.valid_observations[0].event.cost_evidence.confidence = "exact";
  expectIssue(
    validateAttributionV3Corpus(candidate),
    "observation.final_known_meter SDK cost evidence cannot claim exact confidence",
  );
});

test("rejects privacy fixtures that leak a forbidden value", () => {
  const candidate = inputs();
  candidate.corpus.redaction_cases[1].expected.metadata.profile.ssn = "111-22-3333";
  expectIssue(
    validateAttributionV3Corpus(candidate),
    "privacy.hash_task_assignment leaks forbidden value 111-22-3333 into expected output",
  );
});

test("rejects a valid observation missing a required contract field", () => {
  const candidate = inputs();
  delete candidate.corpus.valid_observations[0].event.event_id;
  expectIssue(
    validateAttributionV3Corpus(candidate),
    "observation.final_known_meter valid record fails the observation contract at event_id",
  );
});

test("rejects an invalid mutation that has become valid", () => {
  const candidate = inputs();
  const testCase = candidate.corpus.invalid_observations.find(
    (entry) => entry.id === "observation.invalid.reversed_microseconds",
  );
  testCase.set = {
    "usage_period.start_at": "2026-08-11T10:10:00.123456Z",
    "usage_period.end_at": "2026-08-11T10:10:00.124000Z",
  };
  expectIssue(
    validateAttributionV3Corpus(candidate),
    "observation.invalid.reversed_microseconds mutation remains valid under the observation contract",
  );
});

test("rejects a valid business record missing a required contract field", () => {
  const candidate = inputs();
  delete candidate.corpus.business_identities.valid[0].record.task_id;
  expectIssue(
    validateAttributionV3Corpus(candidate),
    "business.identity.child_assignment valid record fails the business_identity contract at task_id",
  );
});
