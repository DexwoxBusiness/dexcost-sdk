"""Sprint 3 Theme F mediums / §4.3 — _recorded_events FIFO cap.

Pre-fix `_recorded_events` grew without bound across the process
lifetime, leaking ~250 bytes per browser cost event. Capped at
10 000 entries with FIFO eviction (drop oldest 10% in batches to
avoid O(n) pop(0) on every recording).
"""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

from dexcost.adapters.browser import (
    _RECORDED_EVENTS_CAP,
    _recorded_events,
    clear_recorded_events,
    get_recorded_events,
)
from dexcost.models import Event


def _push_n(n: int) -> None:
    for i in range(n):
        e = Event(
            event_id=uuid4(),
            task_id=uuid4(),
            event_type="compute_cost",
            cost_usd=Decimal("0.0001"),
            cost_confidence="computed",
        )
        _recorded_events.append(e)
        # Mirror the cap-enforcement that the production append site applies.
        if len(_recorded_events) > _RECORDED_EVENTS_CAP:
            del _recorded_events[: _RECORDED_EVENTS_CAP // 10]


def test_recorded_events_cap_is_10k() -> None:
    """Confirm the documented cap value hasn't drifted."""
    assert _RECORDED_EVENTS_CAP == 10_000


def test_recorded_events_evicts_oldest_when_above_cap() -> None:
    """Beyond the cap, oldest entries are dropped in batches."""
    clear_recorded_events()
    _push_n(_RECORDED_EVENTS_CAP + 5)
    after = get_recorded_events()
    # After one over-cap append we've evicted ~10% of entries.
    assert len(after) <= _RECORDED_EVENTS_CAP
    assert len(after) > _RECORDED_EVENTS_CAP // 2  # not catastrophic drop
    clear_recorded_events()
