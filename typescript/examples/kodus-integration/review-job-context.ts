/**
 * Per-job ambient attribution for the kodus worker.
 *
 * kodus consumes review jobs from RabbitMQ in the `worker` app. Wrapping
 * each job handler in `runWithContext` gives every job its own ambient
 * session task, labeled with the org/repo as the customer — so all LLM
 * calls, retries, and network egress of one review land on ONE task
 * instead of a process-wide anonymous "agent_session".
 *
 * Use `runWithContext` (scoped), NOT `setContext` (enterWith), in worker
 * loops: consecutive jobs on the same async chain would otherwise inherit
 * the previous job's attribution.
 */

import { runWithContext } from "@dexcost/sdk";

// Illustrative shape — adapt to the actual kodus consumer signature
// (the RabbitMQ handler that kicks off a code review pipeline).
interface ReviewJob {
  organizationId: string;
  repository: string;
  prNumber: number;
}

export async function handleReviewJob(
  job: ReviewJob,
  runReview: (job: ReviewJob) => Promise<void>,
): Promise<void> {
  await runWithContext(
    {
      customerId: job.organizationId,
      projectId: job.repository,
      agent: "kodus_code_review",
      metadata: { pr_number: job.prNumber },
    },
    () => runReview(job),
  );
}

/**
 * Optional next step — explicit mode. Once the ambient setup is proven,
 * graduate the same boundary to `tracker.track()` for exact start/end
 * timing and parent/child nesting:
 *
 *   import { getTracker } from "@dexcost/sdk";
 *
 *   await getTracker().track(
 *     {
 *       taskType: "kodus_code_review",
 *       customerId: job.organizationId,
 *       projectId: job.repository,
 *       metadata: { pr_number: job.prNumber },
 *     },
 *     () => runReview(job),
 *   );
 *
 * Sub-steps wrapped in further track() calls become child tasks
 * (parentTaskId is stamped automatically from the active context).
 */
