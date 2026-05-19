"""Retry heuristic engine — opt-in automatic retry detection (US-036).

Detects likely retries by matching patterns in recent events.  When enabled,
incoming LLM call events are compared against a sliding window of prior events
for the same task.  If a recent event for the same model was marked as failed
(has a transient error type), the new event is flagged as a probable retry with
a confidence score that decays over time.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from dexcost.models.event import Event

# Re-use the canonical error constants from tracker to stay DRY.
# Imported at function level to avoid circular imports; the frozenset
# and dict are module-level there so this is safe.
from dexcost.tracker import _ERROR_LIKELIHOODS, TRANSIENT_ERRORS


@dataclass
class HeuristicMatch:
    """Result of heuristic retry detection."""

    is_retry: bool
    confidence: float  # 0.0 to 1.0
    matched_event_id: uuid.UUID | None  # the suspected original event
    reason: str  # "heuristic"


_NO_MATCH = HeuristicMatch(
    is_retry=False,
    confidence=0.0,
    matched_event_id=None,
    reason="",
)


class RetryHeuristicEngine:
    """Detects likely retries by matching patterns in recent events.

    The engine maintains an in-memory sliding window of events per task.
    When :meth:`check` is called for a new LLM event it looks for the most
    recent event on the same task with the same model that ended in a
    transient error.  If found, a confidence score is computed as::

        confidence = base_likelihood * time_decay

    where ``time_decay = 1.0 - (gap_seconds / window_seconds)``.  If the
    confidence meets the configured threshold the event is flagged as a
    heuristic retry.
    """

    def __init__(
        self,
        window_seconds: float = 30.0,
        threshold: float = 0.8,
    ) -> None:
        if window_seconds <= 0:
            raise ValueError(f"window_seconds must be positive, got {window_seconds}")
        if threshold < 0.0:
            raise ValueError(f"threshold must be non-negative, got {threshold}")
        self._window = timedelta(seconds=window_seconds)
        self._window_seconds = window_seconds
        self._threshold = threshold
        # task_id -> list of events in the window (oldest first)
        self._recent_events: dict[uuid.UUID, list[Event]] = {}

    @property
    def window_seconds(self) -> float:
        """The sliding window duration in seconds."""
        return self._window_seconds

    @property
    def threshold(self) -> float:
        """The minimum confidence to flag a retry."""
        return self._threshold

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record(self, event: Event) -> None:
        """Add *event* to the sliding window for its task."""
        task_id = event.task_id
        if task_id not in self._recent_events:
            self._recent_events[task_id] = []

        self._cleanup_old(task_id, event.occurred_at)
        # _cleanup_old may have removed the key if all events were stale;
        # ensure the list exists before appending.
        if task_id not in self._recent_events:
            self._recent_events[task_id] = []
        self._recent_events[task_id].append(event)

    def check(self, event: Event) -> HeuristicMatch:
        """Check if *event* looks like a retry of a recent failed call.

        Returns a :class:`HeuristicMatch` describing the result.  When no
        match is found, ``is_retry`` is ``False``.
        """
        task_id = event.task_id
        now = event.occurred_at

        events_for_task = self._recent_events.get(task_id, [])
        if not events_for_task:
            return _NO_MATCH

        # Walk backwards to find the most recent event with:
        # - same model
        # - a transient error_type in its details
        for candidate in reversed(events_for_task):
            if candidate.event_id == event.event_id:
                continue
            if candidate.event_type != "llm_call":
                continue
            if candidate.model != event.model:
                continue

            # Check for transient error marker
            error_type = candidate.details.get("error_type")
            if error_type is None or error_type not in TRANSIENT_ERRORS:
                # Found same model but no error — not a retry chain.
                return _NO_MATCH

            # Compute time gap
            gap = (now - candidate.occurred_at).total_seconds()
            if gap < 0 or gap > self._window_seconds:
                return _NO_MATCH

            # Confidence = base_likelihood * time_decay
            # When gap is effectively zero (sub-second), decay is 1.0
            # to avoid floating-point edge cases at threshold boundaries.
            base_likelihood = _ERROR_LIKELIHOODS.get(error_type, 0.8)
            time_decay = max(0.0, 1.0 - (gap / self._window_seconds))
            confidence = base_likelihood * time_decay

            if confidence >= self._threshold:
                return HeuristicMatch(
                    is_retry=True,
                    confidence=confidence,
                    matched_event_id=candidate.event_id,
                    reason="heuristic",
                )

            # Below threshold — not a match
            return _NO_MATCH

        return _NO_MATCH

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _cleanup_old(self, task_id: uuid.UUID, now: datetime) -> None:
        """Remove events older than the window for *task_id*."""
        events = self._recent_events.get(task_id)
        if events is None:
            return

        cutoff = now - self._window
        # Filter in-place: keep only events within the window
        self._recent_events[task_id] = [e for e in events if e.occurred_at >= cutoff]
        if not self._recent_events[task_id]:
            del self._recent_events[task_id]
