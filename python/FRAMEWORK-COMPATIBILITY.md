# Python framework compatibility

Status: Python vNext implementation evidence
Last verified: 2026-08-21

DexCost integrates at documented framework execution and event boundaries. It
does not subclass Pydantic framework models, monkey-patch task methods, or
replace provider drivers.

## CrewAI

Verified against CrewAI 1.15.17 using an installed real `Crew`, `Agent`, `Task`,
`BaseLLM`, event bus, and synchronous kickoff without a provider/network call.

Supported public execution entry points across current Crew, Agent, LiteAgent,
and Flow objects:

- `kickoff`, `kickoff_async`, `akickoff`
- `kickoff_for_each`, `kickoff_for_each_async`, `akickoff_for_each`
- `stream_events`, `astream`
- `resume`, `resume_async`
- `execute_task`, `aexecute_task`, `message`
- `replay`, `train`, `test`
- `ask`, `query_knowledge`, `aquery_knowledge`
- `recall`, `remember`, `extract_memories`

Current `CrewStreamingOutput`, `FlowStreamingOutput`, `StreamSession`, and
`AsyncStreamSession` objects keep their result/lifecycle properties and sync or
async iteration. Filtered stream views such as `events`, `llm`, `messages`,
`flow`, `tools`, `subscribe`, and `interleave` remain tied to the same DexCost
task.

CrewAI's event bus copies `contextvars` into its handler executor and stream
workers. DexCost installs process-wide no-op-unless-active handlers once, then
uses the copied invocation context to prevent cross-run attribution. It calls
the public event-bus flush before task aggregation.

Native `ToolUsageFinishedEvent` and `ToolUsageErrorEvent` records include only
tool name, opaque agent/task/event IDs, cache flag, attempt count, status,
failure type, and CrewAI's exact start/finish duration. Framework failure events
also mark a run failed when an API reports failure without re-raising it.

## Griptape

Verified against installed Griptape 1.12.0 using its real `Agent`,
`Structure.run_stream`, `EventListener`, `EventBus`, prompt/action/structure
events, artifacts, and error lifecycle without a provider/network call.

Supported public Structure entry points:

- `run`
- `run_stream`

The integration uses Griptape's public context-local `EventListener`. It does
not replace OpenAI, Anthropic, Ollama, LiteLLM, embedding, or other drivers, so
provider features and framework tool calling remain intact.

`FinishActionsSubtaskEvent` may describe multiple parallel actions but does not
provide an exact duration for each action. DexCost therefore records zero per
action rather than multiplying the whole subtask duration and overstating tool
time. `FinishStructureRunEvent` with an `ErrorArtifact` marks the canonical task
failed even though an Agent may return normally.

## LLM authority and privacy

OpenAI/Anthropic/LiteLLM/etc. provider instrumentation is authoritative by
default. It has better request correlation, cached-token, provider-cost, and
error fidelity than framework summaries. `capture_llm_events=True` is an
explicit fallback for unsupported custom providers and emits a runtime warning
because enabling both paths for the same call can double count.

Neither integration stores prompt/message content, tool arguments, tool output,
chain-of-thought, model response content, or error messages. Privacy tests place
sentinel secrets in all of those framework fields and assert they never appear
in persisted events or tasks.

## Automated evidence

- `tests/test_framework_runtime.py`: task ownership, context isolation, sync and
  async streams, multiple streams, filtered views, early close, and failures.
- `tests/test_crewai_integration.py`: current execution surface, native tools,
  swallowed framework failures, fallback/deduplication policy, privacy, and
  global/instance APIs.
- `tests/test_crewai_current_compat.py`: opt-in real CrewAI compatibility gate.
- `tests/test_griptape_integration.py`: installed real-package event and Agent
  stream compatibility, action duration policy, failure detection, and privacy.
