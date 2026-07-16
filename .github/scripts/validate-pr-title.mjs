const CONVENTIONAL_PR_TITLE = /^(feat|fix|perf|refactor|docs|test|build|ci|chore|revert|style)(\([a-z0-9][a-z0-9._/-]*\))?!?: [^\s].+$/;

export const TITLE_EXAMPLES = Object.freeze([
  "feat(typescript): emit attribution v2 events",
  "fix(python): restart catalog refresh after fork",
  "chore(main): release typescript 0.12.0",
]);

export function validatePrTitle(title) {
  if (typeof title !== "string" || title.length === 0) {
    return { valid: false, reason: "PR title is missing." };
  }
  if (title.length > 120) {
    return { valid: false, reason: "PR title must be at most 120 characters." };
  }
  if (!CONVENTIONAL_PR_TITLE.test(title)) {
    return {
      valid: false,
      reason:
        "Use Conventional Commit format: type(optional-scope): description. " +
        `Examples: ${TITLE_EXAMPLES.join("; ")}`,
    };
  }
  return { valid: true };
}

if (process.argv[1]?.endsWith("validate-pr-title.mjs")) {
  const result = validatePrTitle(process.env.PR_TITLE);
  if (!result.valid) {
    console.error(`Invalid PR title: ${JSON.stringify(process.env.PR_TITLE ?? "")}`);
    console.error(result.reason);
    process.exitCode = 1;
  } else {
    console.log(`Valid Conventional PR title: ${process.env.PR_TITLE}`);
  }
}
