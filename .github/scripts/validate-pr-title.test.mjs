import assert from "node:assert/strict";
import test from "node:test";

import { validatePrTitle } from "./validate-pr-title.mjs";

for (const title of [
  "feat(typescript): emit attribution v2 events",
  "fix(python): restart catalog refresh after fork",
  "fix: preserve cache accounting",
  "feat!: replace the ingestion contract",
  "chore(main): release typescript 0.12.0",
]) {
  test(`accepts ${title}`, () => {
    assert.equal(validatePrTitle(title).valid, true);
  });
}

for (const title of [
  "Fix Anthropic cache pricing across SDKs",
  "Merge pull request #61 from example/branch",
  "feat(Typescript): uppercase scopes are unstable",
  "feat(typescript) missing colon",
  "feat(typescript): ",
  "",
]) {
  test(`rejects ${title || "an empty title"}`, () => {
    assert.equal(validatePrTitle(title).valid, false);
  });
}

test("rejects titles longer than the repository policy", () => {
  assert.equal(validatePrTitle(`fix(release): ${"x".repeat(121)}`).valid, false);
});
