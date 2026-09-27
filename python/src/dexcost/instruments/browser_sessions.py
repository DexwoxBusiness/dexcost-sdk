"""Durable browser usage, not prices. Never inspect pages or connection URLs.

Bind one provider session/connection to its owning task, then submit complete
terminal meter snapshots. Reconnects that create a new billable connection need
a new ID; polling the same connection never creates another usage event.
"""

from __future__ import annotations

import inspect
import re
from contextlib import suppress
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Literal, cast

from dexcost.context import get_current_task
from dexcost.instruments.database import database_resource_id
from dexcost.models.provider_job import (
    ProviderJobRevision,
    ProviderJobUsageLine,
    provider_job_event_id,
)

_DECIMAL = re.compile(r"(?:0|[1-9][0-9]{0,15})(?:\.[0-9]{1,12})?\Z")


def _identity(service: str, account: str, session: str) -> str:
    if service not in {"browserbase", "browserless"}:
        raise ValueError("Supported browser services: browserbase, browserless")
    return database_resource_id(account, session)


def _date(value: Any) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Provider timestamp must be timezone-aware")
    value = value.astimezone(timezone.utc)
    if value.microsecond % 1000:
        raise ValueError("Provider timestamps must have millisecond precision")
    return cast(datetime, value)


def bind_browser_session(
    tracker: Any,
    *,
    service_key: str,
    billing_account_id: str,
    resource_id: str,
    session_id: str,
    started_at: datetime,
    managed_proxy: bool = False,
) -> bool:
    """Bind in task context using provider start time and opaque account/resource IDs.

    Resource is a Browserbase project or explicitly mapped Browserless API-key/
    fleet ID, never a URL/token. Only provider-owned Browserbase proxy traffic
    may set managed_proxy. Rebinding to a different task/resource is rejected.
    No active task returns False; the caller may bind later in its owning task.
    """
    record = _identity(service_key, billing_account_id, session_id)
    resource = database_resource_id(billing_account_id, resource_id)
    started = _date(started_at)
    if type(managed_proxy) is not bool or (managed_proxy and service_key != "browserbase"):
        raise ValueError("managed_proxy is only valid for Browserbase")
    task = get_current_task()
    if task is None:
        return False
    job = ProviderJobRevision(
        event_id=provider_job_event_id(service_key, "browser", record),
        revision=1,
        task_id=task.task_id,
        provider=service_key,
        service="browser",
        provider_record_id=record,
        operation="browser.session",
        component="external",
        event_type="external_cost",
        resource_type="endpoint",
        resource_id=resource,
        status="submitted",
        submitted_at=started,
        observed_at=started,
        billing_dimensions=(
            ("browser.session_id", session_id),
            ("browser.managed_proxy", "true" if managed_proxy else "false"),
        ),
    )
    previous = tracker.storage.get_provider_job(service_key, "browser", record)
    if previous is not None:
        if replace(previous, revision=1, status="submitted", observed_at=started, usage=()) != job:
            raise ValueError("Browser session already belongs to another task or resource")
        return True
    tracker.storage.insert_provider_job_revision(job)
    return True


def record_browser_session(
    tracker: Any,
    *,
    service_key: str,
    billing_account_id: str,
    session_id: str,
    ended_at: datetime,
    status: Literal["succeeded", "failed", "cancelled"],
    revision: int = 2,
    proxy_bytes: str | None = None,
    time_units: str | None = None,
    proxy_units: str | None = None,
    captcha_units: str | None = None,
) -> bool:
    """Record a complete provider-reported terminal snapshot, without any money.

    Browserbase seconds come from provider start/end timestamps. Browserless
    units must be per-connection billing facts, NOT account totals, Agent Run
    model-token units, local timing, TTLs or request counts. Omitted meters are
    unknown (and removed by a correction), never inferred zero. Binding is
    revision 1; usage starts at 2. Explicitly increment for each correction.
    Old replays return False; conflicting same-revision facts raise. Failed sessions
    can consume units. An unbound session returns False. Invalid facts raise.
    """
    record = _identity(service_key, billing_account_id, session_id)
    ended = _date(ended_at)
    if status not in {"succeeded", "failed", "cancelled"}:
        raise ValueError("Browser session must be terminal")
    previous = tracker.storage.get_provider_job(service_key, "browser", record)
    if previous is None:
        return False
    if type(revision) is not int or revision < 2:
        raise ValueError("Usage revision must be an integer starting at 2")
    if revision < previous.revision:
        return False
    if revision > previous.revision + 1:
        raise ValueError("Usage revisions must be contiguous")
    if ended < previous.submitted_at or ended < previous.observed_at:
        raise ValueError("Provider completion cannot move backwards")
    if service_key == "browserbase" and any(
        v is not None for v in (time_units, proxy_units, captcha_units)
    ):
        raise ValueError("Browserless units are not Browserbase usage")
    if proxy_bytes is not None and (
        service_key != "browserbase"
        or dict(previous.billing_dimensions)["browser.managed_proxy"] != "true"
    ):
        raise ValueError("Proxy bytes require a known Browserbase-managed proxy")
    usage = []
    if service_key == "browserbase":
        elapsed = ended - previous.submitted_at
        seconds = (
            Decimal(elapsed.days * 86400 + elapsed.seconds)
            + Decimal(elapsed.microseconds) / 1_000_000
        )
        if seconds > 0:
            usage.append(ProviderJobUsageLine("browser.session_seconds", seconds, "Seconds"))
    for metric, value, unit in (
        ("browser.proxy_bytes", proxy_bytes, "Bytes"),
        ("browser.time_units", time_units, "Units"),
        ("browser.proxy_units", proxy_units, "Units"),
        ("browser.captcha_units", captcha_units, "Units"),
    ):
        if value is None:
            continue
        if not isinstance(value, str) or not _DECIMAL.fullmatch(value):
            raise ValueError("Browser usage must be a nonnegative plain decimal string")
        quantity = Decimal(value)
        if unit == "Bytes" and quantity != quantity.to_integral_value():
            raise ValueError("Proxy bytes must be integral")
        if quantity > 0:
            usage.append(ProviderJobUsageLine(metric, quantity, unit))
    # A successful operation without positive evidence is not a priced success.
    next_job = replace(
        previous,
        revision=revision,
        observed_at=ended,
        status="unknown" if status == "succeeded" and not usage else status,
        usage=tuple(usage),
    )
    if next_job == previous:
        return True
    tracker.storage.insert_provider_job_revision(next_job)
    return True


def instrument_browserbase(client: Any, tracker: Any, *, billing_account_id: str) -> Any:
    """Return a sync/async facade capturing sessions.create/retrieve/update only.

    A release request or disconnect is NOT completion. Only provider terminal
    status plus ended_at qualifies. Call retrieve after completion; this facade
    makes no extra requests and never polls in the background.
    """
    database_resource_id(billing_account_id, "validation")
    if isinstance(client, _BrowserbaseFacade):
        raise ValueError("Browserbase client is already instrumented")
    return _BrowserbaseFacade(client, tracker, billing_account_id)


def uninstrument_browserbase(client: Any) -> None:
    """Stop local capture only; do not close any remote browser."""
    if isinstance(client, _BrowserbaseFacade):
        client._closed = True


def _field(value: Any, snake: str, camel: str) -> Any:
    return (
        value.get(camel, value.get(snake))
        if isinstance(value, dict)
        else getattr(value, snake, None)
    )


class _BrowserbaseFacade:
    def __init__(self, client: Any, tracker: Any, account: str):
        self._client, self._tracker, self._account = client, tracker, account
        self._closed = False

    def __getattr__(self, name: str) -> Any:
        value = getattr(self._client, name)
        return _SessionsFacade(value, self) if name == "sessions" else value


class _SessionsFacade:
    def __init__(self, sessions: Any, owner: _BrowserbaseFacade):
        self._sessions, self._owner = sessions, owner

    def __getattr__(self, name: str) -> Any:
        fn = getattr(self._sessions, name)
        if name not in {"create", "retrieve", "update"}:
            return fn

        def capture(result: Any, kwargs: dict[str, Any], task: Any) -> Any:
            if self._owner._closed:
                return result
            # Restrict task ownership to the context at invocation, never a poller's task.
            with suppress(Exception):
                from dexcost.context import _current_task

                session = _field(result, "id", "id")
                started = _date(_field(result, "started_at", "startedAt"))
                if name == "create":
                    token = _current_task.set(task)
                    try:
                        bind_browser_session(
                            self._owner._tracker,
                            service_key="browserbase",
                            billing_account_id=self._owner._account,
                            resource_id=_field(result, "project_id", "projectId"),
                            session_id=session,
                            started_at=started,
                            managed_proxy=kwargs.get("proxies") is True,
                        )
                    finally:
                        _current_task.reset(token)
                previous = self._owner._tracker.storage.get_provider_job(
                    "browserbase",
                    "browser",
                    _identity("browserbase", self._owner._account, session),
                )
                if previous is None or previous.submitted_at != started:
                    return result
                # Terminal native responses are captured once. Apply subsequent
                # provider corrections explicitly through record_browser_session,
                # not unordered polling responses without a source revision.
                if previous.terminal:
                    return result
                if previous.resource_id != database_resource_id(
                    self._owner._account, _field(result, "project_id", "projectId")
                ):
                    return result
                status = {"COMPLETED": "succeeded", "ERROR": "failed", "TIMED_OUT": "failed"}.get(
                    _field(result, "status", "status")
                )
                ended = _field(result, "ended_at", "endedAt")
                if status is not None and ended is not None:
                    proxy = _field(result, "proxy_bytes", "proxyBytes")
                    managed = dict(previous.billing_dimensions)["browser.managed_proxy"] == "true"
                    if managed and (type(proxy) is not int or proxy < 0):
                        return result
                    record_browser_session(
                        self._owner._tracker,
                        service_key="browserbase",
                        billing_account_id=self._owner._account,
                        session_id=session,
                        ended_at=_date(ended),
                        status=cast(Literal["succeeded", "failed", "cancelled"], status),
                        proxy_bytes=str(proxy) if managed and type(proxy) is int else None,
                    )
            return result

        def call(*args: Any, **kwargs: Any) -> Any:
            task = get_current_task()
            result = fn(*args, **kwargs)
            if inspect.isawaitable(result):

                async def awaited() -> Any:
                    return capture(await result, kwargs, task)

                return awaited()
            return capture(result, kwargs, task)

        return call
