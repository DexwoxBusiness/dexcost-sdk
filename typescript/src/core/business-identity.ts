import { isoCanonical } from "./models.js";
import type { Task } from "./models.js";

const CANONICAL = /^[a-z0-9][a-z0-9._-]{0,127}$/;

/** Build immutable revision 1 for tasks that opt in with rootTaskId. */
export function toBusinessIdentityRevision(task: Task): Record<string, unknown> | undefined {
  if (task.rootTaskId === undefined) return undefined;
  if (!CANONICAL.test(task.taskType)) throw new Error("business-attributed taskType must be canonical");
  if (task.variant !== undefined && task.experimentId === undefined) {
    throw new Error("variant requires experimentId");
  }
  if ((task.agentId === undefined) !== (task.agentVersion === undefined)) {
    throw new Error("agentId and agentVersion must be supplied together");
  }
  if (task.workflowSessionId !== undefined && task.workflowId === undefined) {
    throw new Error("workflowSessionId requires workflowId");
  }
  if (task.parentTaskId === undefined) {
    if (task.rootTaskId !== task.taskId) throw new Error("a root task must identify itself as rootTaskId");
  } else if (task.rootTaskId === task.taskId || task.parentTaskId === task.taskId) {
    throw new Error("a child task cannot be its own root or parent");
  }
  const assignment: Record<string, string> = {};
  for (const [key, value] of [
    ["customer_id", task.customerId], ["project_id", task.projectId],
    ["user_id", task.userId], ["product_id", task.productId],
    ["experiment_id", task.experimentId], ["variant", task.variant],
  ] as const) if (value !== undefined) assignment[key] = value;
  const hierarchy: Record<string, string> = {
    task_type: task.taskType, root_task_id: task.rootTaskId,
  };
  if (task.parentTaskId !== undefined) hierarchy["parent_task_id"] = task.parentTaskId;
  const timestamp = isoCanonical(task.startedAt);
  const result: Record<string, unknown> = {
    schema_version: "1", task_id: task.taskId, revision: 1,
    effective_at: timestamp, observed_at: timestamp,
    identity_snapshot: "full", task: hierarchy, assignment,
  };
  if (task.workflowId !== undefined) {
    result["workflow"] = {
      id: task.workflowId,
      ...(task.workflowSessionId === undefined ? {} : { session_id: task.workflowSessionId }),
    };
  }
  if (task.agentId !== undefined && task.agentVersion !== undefined) {
    result["agent"] = { id: task.agentId, version: task.agentVersion };
  }
  return result;
}
