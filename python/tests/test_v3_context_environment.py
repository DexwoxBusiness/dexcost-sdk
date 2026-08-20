"""Attribution v3 in-place extensions: environment, user/product, latency.

Covers the three optional fields the ingestion boundary gained without a
wire ``schema_version`` bump:

* top-level ``environment``
* ``operation.latency_ms``
* business identity ``assignment.user_id`` / ``assignment.product_id``
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from dexcost.attribution.convert import to_business_identity_revision_v1
from dexcost.attribution.v3_convert import (
    _MAX_LATENCY_MS,
    to_attribution_observation_v3,
)
from dexcost.auto_task import create_auto_task
from dexcost.config import DexcostConfig
from dexcost.context import (
    clear_context,
    get_context,
    set_context,
    set_current_task,
)
from dexcost.models.event import Event
from dexcost.models.task import Task
from dexcost.storage.sqlite import SQLiteStorage
from dexcost.sync import SyncWorker


def _llm_event(**overrides: object) -> Event:
    fields: dict[str, object] = {
        "task_id": uuid.uuid4(),
        "event_type": "llm_call",
        "provider": "openai",
        "model": "gpt-4o",
        "input_tokens": 100,
        "output_tokens": 20,
        "cost_usd": Decimal("0.001"),
        "cost_confidence": "estimated",
        "pricing_source": "sdk_catalog",
        "pricing_version": "2026-01-01",
    }
    fields.update(overrides)
    return Event(**fields)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _clean_context():
    # Other suites can leak an active task into the contextvar; a leaked
    # parent would silently re-home the tasks built here.
    clear_context()
    set_current_task(None)
    yield
    clear_context()
    set_current_task(None)


# ---------------------------------------------------------------------------
# 1. environment
# ---------------------------------------------------------------------------


class TestEnvironmentSerialization:
    def test_environment_is_serialized_at_the_top_level(self) -> None:
        observation = to_attribution_observation_v3(_llm_event(), environment="production")

        assert observation is not None
        assert observation["environment"] == "production"

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("  Production ", "production"),
            ("STAGING", "staging"),
            ("eu-west-1.prod", "eu-west-1.prod"),
            ("dev_2", "dev_2"),
        ],
    )
    def test_environment_is_lowercased_and_stripped(self, raw: str, expected: str) -> None:
        observation = to_attribution_observation_v3(_llm_event(), environment=raw)

        assert observation is not None
        assert observation["environment"] == expected

    def test_environment_is_omitted_when_not_configured(self) -> None:
        observation = to_attribution_observation_v3(_llm_event())

        assert observation is not None
        assert "environment" not in observation

    @pytest.mark.parametrize(
        "raw",
        [
            "prod env",  # space is outside the server charset
            "-prod",  # must start alphanumeric
            "prod!",  # punctuation outside [._-]
            "prod/eu",
            "p" * 65,  # 1..64 characters
            "",
            "   ",
        ],
    )
    def test_invalid_environment_is_dropped_without_raising(self, raw: str) -> None:
        observation = to_attribution_observation_v3(_llm_event(), environment=raw)

        # The rest of the observation must survive intact — dropping a bad
        # environment must never cost the caller their cost data.
        assert observation is not None
        assert "environment" not in observation
        assert observation["schema_version"] == "3"
        assert observation["usage"]

    def test_non_string_environment_is_dropped_without_raising(self) -> None:
        observation = to_attribution_observation_v3(
            _llm_event(), environment=object()  # type: ignore[arg-type]
        )

        assert observation is not None
        assert "environment" not in observation

    def test_invalid_environment_is_logged_not_raised(self, caplog) -> None:
        with caplog.at_level(logging.WARNING, logger="dexcost.attribution.v3_convert"):
            observation = to_attribution_observation_v3(
                _llm_event(), environment="prod env"
            )

        assert observation is not None
        assert any("environment" in record.message for record in caplog.records)

    def test_sync_worker_threads_the_configured_environment(self, tmp_path) -> None:
        config = DexcostConfig(storage="local", environment="Staging")
        storage = SQLiteStorage(db_path=str(tmp_path / "buffer.db"))
        worker = SyncWorker(config=config, storage=storage)
        try:
            prepared = worker._prepare_event_dict(_llm_event())
        finally:
            storage.close()

        assert prepared is not None
        assert prepared["environment"] == "staging"

    def test_sync_worker_omits_environment_when_unset(self, tmp_path, monkeypatch) -> None:
        monkeypatch.delenv("DEXCOST_ENV", raising=False)
        config = DexcostConfig(storage="local")
        storage = SQLiteStorage(db_path=str(tmp_path / "buffer.db"))
        worker = SyncWorker(config=config, storage=storage)
        try:
            prepared = worker._prepare_event_dict(_llm_event())
        finally:
            storage.close()

        assert prepared is not None
        assert "environment" not in prepared


# ---------------------------------------------------------------------------
# 2. user_id / product_id
# ---------------------------------------------------------------------------


def _business_task(**overrides: object) -> Task:
    task_id = uuid.uuid4()
    fields: dict[str, object] = {
        "task_id": task_id,
        "task_type": "resolve_ticket",
        "status": "success",
        "started_at": datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc),
        "root_task_id": task_id,
    }
    fields.update(overrides)
    return Task(**fields)  # type: ignore[arg-type]


class TestUserAndProductContext:
    def test_set_context_accepts_user_and_product(self) -> None:
        set_context(customer_id="acme", user_id="user-42", product_id="chat-widget")

        ctx = get_context()
        assert ctx is not None
        assert ctx.user_id == "user-42"
        assert ctx.product_id == "chat-widget"

    @pytest.mark.parametrize("field", ["user_id", "product_id"])
    @pytest.mark.parametrize("bad", ["", "   ", "x" * 257])
    def test_set_context_validates_like_customer_id(self, field: str, bad: str) -> None:
        with pytest.raises(ValueError, match=field):
            set_context(**{field: bad})

    def test_public_set_context_forwards_user_and_product(self) -> None:
        import dexcost

        dexcost.set_context(user_id="user-42", product_id="chat-widget")

        ctx = get_context()
        assert ctx is not None
        assert ctx.user_id == "user-42"
        assert ctx.product_id == "chat-widget"

    def test_identity_assignment_carries_user_and_product(self) -> None:
        task = _business_task(user_id="user-42", product_id="chat-widget")

        identity = to_business_identity_revision_v1(task)

        assert identity is not None
        assert identity["assignment"]["user_id"] == "user-42"
        assert identity["assignment"]["product_id"] == "chat-widget"

    def test_identity_assignment_omits_unset_user_and_product(self) -> None:
        identity = to_business_identity_revision_v1(_business_task(customer_id="acme"))

        assert identity is not None
        assert "user_id" not in identity["assignment"]
        assert "product_id" not in identity["assignment"]

    def test_auto_task_inherits_user_and_product_from_context(self) -> None:
        set_context(user_id="user-42", product_id="chat-widget")

        task = create_auto_task("llm_call")

        assert task.user_id == "user-42"
        assert task.product_id == "chat-widget"
        # Business identity present ⇒ the auto-task becomes its own root so
        # the identity revision is deliverable.
        assert task.root_task_id == task.task_id

    def test_tracker_task_reaches_the_identity_assignment(self, tmp_path) -> None:
        from dexcost.tracker import CostTracker

        storage = SQLiteStorage(db_path=str(tmp_path / "buffer.db"))
        tracker = CostTracker(storage=storage, auto_instrument=[])
        try:
            with tracker.task(
                task_type="resolve_ticket",
                customer_id="acme",
                user_id="user-42",
                product_id="chat-widget",
            ) as tracked:
                task = tracked._task
        finally:
            storage.close()

        identity = to_business_identity_revision_v1(task)
        assert identity is not None
        assert identity["assignment"]["user_id"] == "user-42"
        assert identity["assignment"]["product_id"] == "chat-widget"

    def test_user_and_product_survive_the_sqlite_buffer(self, tmp_path) -> None:
        storage = SQLiteStorage(db_path=str(tmp_path / "buffer.db"))
        task = _business_task(user_id="user-42", product_id="chat-widget")
        try:
            storage.insert_task(task)
            restored = storage.get_task(str(task.task_id))
        finally:
            storage.close()

        assert restored is not None
        assert restored.user_id == "user-42"
        assert restored.product_id == "chat-widget"

    def test_user_and_product_round_trip_through_to_dict(self) -> None:
        task = _business_task(user_id="user-42", product_id="chat-widget")

        restored = Task.from_dict(task.to_dict())

        assert restored.user_id == "user-42"
        assert restored.product_id == "chat-widget"


# ---------------------------------------------------------------------------
# 3. operation.latency_ms
# ---------------------------------------------------------------------------


class TestOperationLatency:
    def test_latency_is_serialized_on_the_operation(self) -> None:
        observation = to_attribution_observation_v3(_llm_event(latency_ms=1234))

        assert observation is not None
        assert observation["operation"]["latency_ms"] == 1234

    def test_latency_is_clamped_at_the_upper_bound(self) -> None:
        observation = to_attribution_observation_v3(
            _llm_event(latency_ms=_MAX_LATENCY_MS + 1_000)
        )

        assert observation is not None
        assert observation["operation"]["latency_ms"] == _MAX_LATENCY_MS

    def test_latency_at_the_upper_bound_is_preserved(self) -> None:
        observation = to_attribution_observation_v3(_llm_event(latency_ms=_MAX_LATENCY_MS))

        assert observation is not None
        assert observation["operation"]["latency_ms"] == _MAX_LATENCY_MS

    def test_negative_latency_is_clamped_to_zero(self) -> None:
        observation = to_attribution_observation_v3(_llm_event(latency_ms=-5))

        assert observation is not None
        assert observation["operation"]["latency_ms"] == 0

    def test_latency_is_omitted_when_none(self) -> None:
        observation = to_attribution_observation_v3(_llm_event(latency_ms=None))

        assert observation is not None
        assert "latency_ms" not in observation["operation"]

    @pytest.mark.parametrize("bad", ["fast", object(), float("nan"), float("inf")])
    def test_invalid_latency_is_omitted_without_raising(self, bad: object) -> None:
        observation = to_attribution_observation_v3(_llm_event(latency_ms=bad))

        assert observation is not None
        assert "latency_ms" not in observation["operation"]

    def test_latency_falls_back_to_details(self) -> None:
        observation = to_attribution_observation_v3(
            _llm_event(latency_ms=None, details={"latency_ms": 77})
        )

        assert observation is not None
        assert observation["operation"]["latency_ms"] == 77


# ---------------------------------------------------------------------------
# All three together
# ---------------------------------------------------------------------------


def test_all_three_extensions_on_one_observation() -> None:
    observation = to_attribution_observation_v3(
        _llm_event(latency_ms=1234), environment="Production"
    )

    assert observation is not None
    assert observation["environment"] == "production"
    assert observation["operation"]["latency_ms"] == 1234
    assert observation["schema_version"] == "3"
