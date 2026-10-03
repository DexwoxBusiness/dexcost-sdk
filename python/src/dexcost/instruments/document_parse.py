"""Hosted LlamaParse v2 credit evidence; never document contents or cash estimates."""

from __future__ import annotations

import inspect
import re
from contextlib import suppress
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, cast

from dexcost.context import _current_task, get_current_task
from dexcost.instruments._capture import provider_capture_callable
from dexcost.instruments.database import database_resource_id
from dexcost.instruments.web_tools import _field, _id, _record
from dexcost.models.provider_job import (
    ProviderJobRevision,
    ProviderJobUsageLine,
    provider_job_event_id,
)

_TIERS = {"fast", "cost_effective", "agentic", "agentic_plus"}


def _timestamp(value: Any, *, end: bool = False) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("LlamaParse timestamps must be timezone-aware")
    value = value.astimezone(timezone.utc)
    remainder = value.microsecond % 1000
    # The wire contract is millisecond-precision: widen, never shorten an interval.
    return cast(
        datetime,
        (
            value
            - timedelta(microseconds=remainder)
            + (timedelta(milliseconds=1) if end and remainder else timedelta())
        ),
    )


def _job(response: Any) -> Any:
    job = _field(response, "job")
    return job if job is not None else response


def _identity(response: Any, account: str, project: str) -> tuple[Any, str, str, datetime]:
    job = _job(response)
    if _id(_field(job, "project_id")) != _id(project):
        raise ValueError("LlamaParse job does not belong to the mapped project")
    return (
        job,
        database_resource_id(account, _id(_field(job, "id"))),
        database_resource_id(account, project),
        _timestamp(_field(job, "created_at")),
    )


def bind_llamaparse_job(
    tracker: Any,
    response: Any,
    *,
    billing_account_id: str,
    project_id: str,
    tier: str | None = None,
) -> bool:
    """Bind a v2 create/parse response in its initiating task, using provider time.

    The explicit opaque account mapping must identify this hosted client's billing
    account. Project is checked against the response. Supply the requested tier if
    create omits it; configured/unknown tiers cannot be guessed. Cached jobs keep
    their original owner. No file, URL, name, user metadata or credentials persist.
    """
    job, record, resource, started = _identity(response, billing_account_id, project_id)
    resolved_tier = _field(job, "tier") or tier
    if resolved_tier not in _TIERS:
        return False
    task = get_current_task()
    if task is None:
        return False
    new = ProviderJobRevision(
        event_id=provider_job_event_id("llamaparse", "parse", record),
        revision=1,
        task_id=task.task_id,
        provider="llamaparse",
        service="parse",
        provider_record_id=record,
        operation="llamaparse.parse",
        component="external",
        event_type="external_cost",
        resource_type="endpoint",
        resource_id=resource,
        status="submitted",
        submitted_at=started,
        observed_at=started,
        billing_dimensions=(("parse_tier", resolved_tier),),
    )
    old = tracker.storage.get_provider_job("llamaparse", "parse", record)
    if old is not None:
        if replace(old, revision=1, status="submitted", observed_at=started, usage=()) != new:
            raise ValueError("LlamaParse job ownership or identity changed")
        return True
    tracker.storage.insert_provider_job_revision(new)
    return True


def record_llamaparse_job(
    tracker: Any, response: Any, *, billing_account_id: str, project_id: str, revision: int = 2
) -> bool:
    """Full completed Parse v2 snapshot with expand=usage; missing credits != zero.

    Replayed native reads never accumulate charges. Later corrections require the
    explicit next revision. Provider created_at/updated_at define the conservative
    interval, not the time an archived response is imported. Money comes only from
    a reconciled parse-only invoice pool, never credits multiplied by a list rate.
    """
    job, record, resource, started = _identity(response, billing_account_id, project_id)
    old = tracker.storage.get_provider_job("llamaparse", "parse", record)
    if old is None:
        return False
    if old.resource_id != resource or old.submitted_at != started:
        raise ValueError("LlamaParse job identity changed")
    if _field(job, "status") != "COMPLETED":
        return False
    tier = _field(job, "tier")
    if tier is None:
        return False
    if (("parse_tier", tier),) != old.billing_dimensions:
        raise ValueError("LlamaParse job tier changed")
    value = _field(_field(job, "usage"), "credits")
    if value is None:
        return False
    if type(value) not in {int, float, str, Decimal}:
        raise ValueError("LlamaParse credits must be an exact nonnegative decimal")
    if (
        isinstance(value, str)
        and re.fullmatch(r"(?:0|[1-9][0-9]{0,15})(?:\.[0-9]{1,32})?", value) is None
    ):
        raise ValueError("LlamaParse credit strings must be plain decimals")
    amount = Decimal(str(value))
    if (
        not amount.is_finite()
        or amount < 0
        or amount > 9007199254740991
        or cast(int, amount.normalize().as_tuple().exponent) < -12
    ):
        raise ValueError("LlamaParse credits must fit the exact 12-decimal quantity domain")
    usage = (ProviderJobUsageLine("llamaparse.credits", amount, "Credits"),) if amount else ()
    return _record(
        tracker, old, revision, _timestamp(_field(job, "updated_at"), end=True), "succeeded", usage
    )


class _LlamaParseFacade:
    def __init__(
        self,
        client: Any,
        tracker: Any,
        account: str,
        project: str,
        state: list[bool] | None = None,
        parsing: bool = False,
    ) -> None:
        self._client, self._tracker, self._account, self._project = (
            client,
            tracker,
            account,
            project,
        )
        self._state, self._parsing = state if state is not None else [True], parsing

    def __getattr__(self, name: str) -> Any:
        native = getattr(self._client, name)
        if not self._parsing and name == "parsing" and self._state[0]:
            return _LlamaParseFacade(
                native, self._tracker, self._account, self._project, self._state, True
            )
        if not self._parsing or name not in {"create", "get", "parse"} or not callable(native):
            return native

        def invoke(*args: Any, **kwargs: Any) -> Any:
            task = get_current_task()

            def capture(result: Any) -> Any:
                if not self._state[0]:
                    return result
                with suppress(Exception):
                    token = _current_task.set(task)
                    try:
                        if name in {"create", "parse"}:
                            bind_llamaparse_job(
                                self._tracker,
                                result,
                                billing_account_id=self._account,
                                project_id=self._project,
                                tier=kwargs.get("tier"),
                            )
                        _, record, _, _ = _identity(result, self._account, self._project)
                        old = self._tracker.storage.get_provider_job("llamaparse", "parse", record)
                        if old is not None and old.revision == 1:
                            record_llamaparse_job(
                                self._tracker,
                                result,
                                billing_account_id=self._account,
                                project_id=self._project,
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

        guarded = provider_capture_callable("llamaparse", invoke, native)

        def dispatch(*args: Any, **kwargs: Any) -> Any:
            return guarded(*args, **kwargs) if self._state[0] else native(*args, **kwargs)

        return dispatch


def instrument_llamaparse(
    client: Any, tracker: Any, *, billing_account_id: str, project_id: str
) -> Any:
    """Wrap hosted llama-cloud LlamaCloud/AsyncLlamaCloud parsing.create/get/parse.

    Request expand=['usage', ...] yourself; no options, polling, or requests are
    added. Null credits after completion remain pending until your later get.
    Raw/streaming wrappers, legacy v1, Extract, Index and self-hosted are excluded.
    """
    return _LlamaParseFacade(client, tracker, _id(billing_account_id), _id(project_id))


def uninstrument_llamaparse(client: Any) -> None:
    if isinstance(client, _LlamaParseFacade):
        client._state[0] = False
