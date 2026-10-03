"""Explicit native-response capture for Firecrawl and Apify; no payloads or prices.

Bind an asynchronous job in its owning task before recording it elsewhere.
Records are full, explicitly revisioned snapshots, never sums of polling replies.
"""

from __future__ import annotations

import inspect
from contextlib import suppress
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Literal, cast

from dexcost.context import get_current_task
from dexcost.instruments._capture import provider_capture_callable
from dexcost.instruments.browser_sessions import _date
from dexcost.instruments.database import database_resource_id
from dexcost.models.provider_job import (
    ProviderJobRevision,
    ProviderJobStatus,
    ProviderJobUsageLine,
    provider_job_event_id,
)


def _field(value: Any, name: str, snake: str | None = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, value.get(snake or name))
    return getattr(value, snake or name, None)


def _id(value: Any) -> str:
    # Validate an opaque ID, never a URL, token or user-supplied page path.
    if not isinstance(value, str):
        raise ValueError("Provider ID must be a string")
    database_resource_id("validation", value)
    return value


def _now() -> datetime:
    value = datetime.now(timezone.utc)
    return value.replace(microsecond=(value.microsecond // 1000) * 1000)


def _bind(
    tracker: Any,
    provider: str,
    service: str,
    record: str,
    resource: str,
    started: datetime,
    operation: str,
) -> bool:
    task = get_current_task()
    if task is None:
        return False
    job = ProviderJobRevision(
        event_id=provider_job_event_id(provider, service, record),
        revision=1,
        task_id=task.task_id,
        provider=provider,
        service=service,
        provider_record_id=record,
        operation=operation,
        component="external",
        event_type="external_cost",
        resource_type="endpoint",
        resource_id=resource,
        status="submitted",
        submitted_at=started,
        observed_at=started,
    )
    old = tracker.storage.get_provider_job(provider, service, record)
    if old is not None:
        if replace(old, revision=1, status="submitted", observed_at=started, usage=()) != job:
            raise ValueError("Provider run belongs to another task, resource or operation")
        return True
    tracker.storage.insert_provider_job_revision(job)
    return True


def _record(
    tracker: Any,
    old: ProviderJobRevision,
    revision: int,
    ended: datetime,
    status: ProviderJobStatus,
    usage: tuple[ProviderJobUsageLine, ...],
) -> bool:
    if type(revision) is not int or revision < 2:
        raise ValueError("Terminal usage revisions start at 2")
    if revision < old.revision:
        return False
    if revision > old.revision + 1:
        raise ValueError("Usage revisions must be contiguous")
    if ended < old.submitted_at or ended < old.observed_at:
        raise ValueError("Provider completion cannot move backwards")
    job = replace(
        old,
        revision=revision,
        observed_at=ended,
        status="unknown" if status == "succeeded" and not usage else status,
        usage=usage,
    )
    if job != old:
        tracker.storage.insert_provider_job_revision(job)
    return True


def bind_apify_run(tracker: Any, run: Any) -> bool:
    """Bind the native actor.start/call result in the initiating task.

    Reads id, actId and startedAt only. A later get/wait response must not bind
    itself to the polling task. API run IDs are the server reconciliation key.
    """
    return _bind(
        tracker,
        "apify",
        "actor_runs",
        _id(_field(run, "id")),
        _id(_field(run, "actId")),
        _date(_field(run, "startedAt")),
        "actor.run",
    )


def record_apify_run(tracker: Any, run: Any, *, revision: int = 2) -> bool:
    """Record terminal identity, not usageTotalUsd. Server authenticated import
    supplies stable run costs, including zero and later corrections. No polling.
    """
    run_id = _id(_field(run, "id"))
    old = tracker.storage.get_provider_job("apify", "actor_runs", run_id)
    if old is None:
        return False
    if old.resource_id != _id(_field(run, "actId")) or old.submitted_at != _date(
        _field(run, "startedAt")
    ):
        raise ValueError("Apify run identity changed")
    status = {
        "SUCCEEDED": "succeeded",
        "FAILED": "failed",
        "TIMED-OUT": "failed",
        "ABORTED": "cancelled",
    }.get(_field(run, "status"))
    if status is None:
        return False
    return _record(
        tracker,
        old,
        revision,
        _date(_field(run, "finishedAt")),
        cast(ProviderJobStatus, status),
        (ProviderJobUsageLine("apify.run_count", 1, "Runs"),),
    )


def bind_firecrawl_job(
    tracker: Any,
    *,
    billing_account_id: str,
    resource_id: str,
    job_id: str,
    started_at: datetime,
    operation: Literal["crawl", "batch_scrape"],
) -> bool:
    """Bind an async job using its provider createdAt and opaque team/resource IDs.

    Call in the initiating task with metadata from the first status response.
    No page content, URLs, account credentials, or account-wide credit balances.
    """
    if operation not in {"crawl", "batch_scrape"}:
        raise ValueError("Unsupported Firecrawl operation")
    record = database_resource_id(billing_account_id, job_id)
    return _bind(
        tracker,
        "firecrawl",
        "web",
        record,
        database_resource_id(billing_account_id, resource_id),
        _date(started_at),
        f"firecrawl.{operation}",
    )


def record_firecrawl_job(
    tracker: Any, response: Any, *, billing_account_id: str, job_id: str, revision: int = 2
) -> bool:
    """Record v2 crawl/batch status creditsUsed once; never count pages or polls.

    Requires terminal createdAt/completedAt and exact nonnegative integral
    creditsUsed. Missing meters remain unknown. Replays are idempotent;
    corrections require an explicit next revision, including nonzero/zero/restore.
    """
    record = database_resource_id(billing_account_id, job_id)
    old = tracker.storage.get_provider_job("firecrawl", "web", record)
    if old is None:
        return False
    status = {"completed": "succeeded", "failed": "failed", "cancelled": "cancelled"}.get(
        _field(response, "status")
    )
    if status is None:
        return False
    if old.submitted_at != _date(_field(response, "createdAt", "created_at")):
        raise ValueError("Firecrawl job identity changed")
    credits = _field(response, "creditsUsed", "credits_used")
    if type(credits) is not int or not 0 <= credits <= 9_007_199_254_740_991:
        raise ValueError("Firecrawl creditsUsed must be an exact nonnegative integer")
    usage = (ProviderJobUsageLine("firecrawl.credits", credits, "Credits"),) if credits else ()
    return _record(
        tracker,
        old,
        revision,
        _date(_field(response, "completedAt", "completed_at")),
        cast(ProviderJobStatus, status),
        usage,
    )


def record_firecrawl_search(
    tracker: Any,
    response: Any,
    *,
    billing_account_id: str,
    resource_id: str,
    occurred_at: datetime,
    observed_at: datetime,
    revision: int = 2,
) -> bool:
    """Record a synchronous v2 search response in its owning task.

    Exact response credits only. The account-scoped resource must match the
    reconciled Firecrawl invoice mapping. Provider id prevents replay charges.
    Search results, request text, scrapeOptions, and page bodies are not retained.
    Supply the original request start and response-observed finish, not the time
    an archived response is imported. Unknown request timing is not allocatable.
    """
    return _record_firecrawl_request(
        tracker,
        response,
        billing_account_id=billing_account_id,
        resource_id=resource_id,
        occurred_at=occurred_at,
        observed_at=observed_at,
        revision=revision,
        operation="firecrawl.search",
    )


def _record_firecrawl_request(
    tracker: Any,
    response: Any,
    *,
    billing_account_id: str,
    resource_id: str,
    occurred_at: datetime,
    observed_at: datetime,
    revision: int = 2,
    operation: str,
) -> bool:
    if _field(response, "success") is not True:
        return False
    record = database_resource_id(billing_account_id, _id(_field(response, "id")))
    resource = database_resource_id(billing_account_id, resource_id)
    credits = _field(response, "creditsUsed", "credits_used")
    if type(credits) is not int or not 0 <= credits <= 9_007_199_254_740_991:
        raise ValueError("Firecrawl creditsUsed must be an exact nonnegative integer")
    started, ended = _date(occurred_at), _date(observed_at)
    if ended < started:
        raise ValueError("Response observation cannot precede request start")
    if not _bind(tracker, "firecrawl", "web", record, resource, started, operation):
        return False
    old = tracker.storage.get_provider_job("firecrawl", "web", record)
    usage = (ProviderJobUsageLine("firecrawl.credits", credits, "Credits"),) if credits else ()
    return _record(tracker, old, revision, ended, "succeeded", usage)


class _NativeWebTools:
    """Non-mutating facade: native receivers, return values and errors are preserved."""

    def __init__(
        self,
        client: Any,
        tracker: Any,
        account: str,
        resource: str,
        provider: str,
        kind: str = "root",
        state: list[bool] | None = None,
    ) -> None:
        self._client, self._tracker = client, tracker
        self._account, self._resource = account, resource
        self._provider, self._kind = provider, kind
        self._state = state if state is not None else [True]

    def __getattr__(self, name: str) -> Any:
        native = getattr(self._client, name)
        if not callable(native):
            return native
        if self._provider == "apify" and self._kind == "root" and name in {"actor", "task", "run"}:

            def child(*args: Any, **kwargs: Any) -> Any:
                result = native(*args, **kwargs)
                if not self._state[0]:
                    return result
                return _NativeWebTools(
                    result,
                    self._tracker,
                    self._account,
                    self._resource,
                    "apify",
                    name,
                    self._state,
                )

            return child
        enabled = (self._provider == "firecrawl" and name in {"scrape", "search"}) or (
            self._provider == "apify"
            and (
                (self._kind in {"actor", "task"} and name in {"start", "call"})
                or (self._kind == "run" and name in {"get", "wait_for_finish"})
            )
        )
        if not enabled:
            return native

        def invoke(*args: Any, **kwargs: Any) -> Any:
            task = get_current_task()
            started = _now()

            def capture(result: Any) -> Any:
                ended = _now()
                if not self._state[0]:
                    return result
                # Instrumentation errors cannot alter the provider operation.
                with suppress(Exception):
                    from dexcost.context import _current_task

                    token = _current_task.set(task)
                    try:
                        if self._provider == "apify":
                            # Explicit provider account ID, not an inferred poller's account.
                            if _field(result, "userId") != self._account:
                                return result
                            if self._kind in {"actor", "task"}:
                                bind_apify_run(self._tracker, result)
                            old = self._tracker.storage.get_provider_job(
                                "apify", "actor_runs", _id(_field(result, "id"))
                            )
                            if old is not None and old.revision == 1:
                                record_apify_run(self._tracker, result)
                        elif name == "scrape":
                            metadata = _field(result, "metadata")
                            # The native Document exposes an exact meter and stable scrape ID.
                            payload = {
                                "success": True,
                                "id": _field(metadata, "scrapeId", "scrape_id"),
                                "creditsUsed": _field(metadata, "creditsUsed", "credits_used"),
                            }
                            _record_firecrawl_request(
                                self._tracker,
                                payload,
                                billing_account_id=self._account,
                                resource_id=self._resource,
                                occurred_at=started,
                                observed_at=ended,
                                operation="firecrawl.scrape",
                            )
                        else:
                            # Current Python SearchData strips these fields: remain unknown.
                            payload = {
                                "success": True,
                                "id": _field(result, "id"),
                                "creditsUsed": _field(result, "creditsUsed", "credits_used"),
                            }
                            record_firecrawl_search(
                                self._tracker,
                                payload,
                                billing_account_id=self._account,
                                resource_id=self._resource,
                                occurred_at=started,
                                observed_at=ended,
                            )
                    finally:
                        _current_task.reset(token)
                return result

            result = native(*args, **kwargs)
            if inspect.isawaitable(result):

                async def awaited() -> Any:
                    return capture(await result)

                return awaited()
            return capture(result)

        guarded = provider_capture_callable(self._provider, invoke, native)

        def dispatch(*args: Any, **kwargs: Any) -> Any:
            return guarded(*args, **kwargs) if self._state[0] else native(*args, **kwargs)

        return dispatch


def instrument_apify(client: Any, tracker: Any, *, billing_account_id: str) -> Any:
    """Wrap ApifyClient/ApifyClientAsync. Account is the native run's userId.

    Captures actor/task start/call ownership and run get/wait_for_finish terminal
    identity. No polling is added; a task must eventually observe its completion.
    Native USD values are ignored. Re-import server run costs for later corrections.
    """
    return _NativeWebTools(client, tracker, _id(billing_account_id), "", "apify")


def uninstrument_apify(client: Any) -> None:
    if isinstance(client, _NativeWebTools) and client._provider == "apify":
        client._state[0] = False


def instrument_firecrawl(
    client: Any, tracker: Any, *, billing_account_id: str, resource_id: str
) -> Any:
    """Wrap a hosted Firecrawl v2 client, mapping its key to an opaque account/resource.

    Scrape requires metadata.scrape_id + credits_used. Search requires preserved
    id + creditsUsed (current Python SearchData omits these). Missing meters stay
    unknown. Async jobs use explicit bind/record helpers with provider timestamps.
    Does not read credentials, URLs, documents or add any provider requests.
    """
    return _NativeWebTools(client, tracker, _id(billing_account_id), _id(resource_id), "firecrawl")


def uninstrument_firecrawl(client: Any) -> None:
    if isinstance(client, _NativeWebTools) and client._provider == "firecrawl":
        client._state[0] = False
