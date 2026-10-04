"""Explicit final provider zero is distinct from absent or computed cost."""

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from dexcost.instruments._provider_metering import OperationMeasurement, ProviderUsageLine
from dexcost.models.provider_job import (
    ProviderJobRevision,
    ProviderJobUsageLine,
    provider_job_event_id,
)
from dexcost.provider_jobs import _measurement_fields
from dexcost.storage.sqlite import SQLiteStorage
from dexcost.tracker import CostTracker


def job():
    now = datetime.now(timezone.utc)
    return ProviderJobRevision(
        event_id=provider_job_event_id("perplexity", "responses", "zero-job"),
        revision=1,
        submitted_at=now,
        observed_at=now,
        task_id=uuid4(),
        provider="perplexity",
        service="responses",
        provider_record_id="zero-job",
        operation="perplexity.responses.create",
        component="llm",
        event_type="llm_call",
        resource_type="model",
        resource_id="perplexity/fixture",
        status="succeeded",
        usage=(ProviderJobUsageLine("request_count", Decimal(1), "Requests"),),
        cost_amount=Decimal(0),
        cost_source="provider_reported",
        cost_confidence="exact",
    )


def test_explicit_zero_model_and_storage(tmp_path):
    storage = SQLiteStorage(str(tmp_path / "zero.db"))
    tracker = CostTracker(storage=storage, auto_update_pricing=False, auto_instrument=[])
    try:
        measured = OperationMeasurement(
            pricing_usage={},
            usage_lines=(ProviderUsageLine("request_count", 1, "Requests"),),
            provider_cost_usd="0",
        )
        fields = _measurement_fields(tracker, "fixture", measured)
        assert fields["cost_amount"] == Decimal(0)
        assert fields["cost_source"] == "provider_reported"
        assert (
            _measurement_fields(tracker, "fixture", replace(measured, provider_cost_usd=None))[
                "cost_amount"
            ]
            is None
        )
        revision = job()
        storage.insert_provider_job_revision(revision)
        restored = storage.get_provider_job("perplexity", "responses", "zero-job")
        assert restored.to_dict()["cost_amount"] == "0"
        storage.insert_provider_job_revision(revision)
        assert len(storage.query_provider_jobs_for_sync()) == 1
    finally:
        storage.close()


def test_explicit_zero_v3_wire():
    assert job().to_attribution_observation()["cost_evidence"] == {
        "amount": "0",
        "currency": "USD",
        "source": "provider_reported",
        "confidence": "exact",
    }


@pytest.mark.parametrize(
    "fields",
    [
        {"cost_confidence": "estimated"},
        {
            "cost_source": "sdk_catalog",
            "cost_confidence": "computed",
            "pricing_version": "fixture",
        },
        {"status": "failed"},
        {"status": "cancelled"},
    ],
)
def test_reject_non_final_or_synthesized_zero(fields):
    with pytest.raises(ValueError):
        replace(job(), **fields)
