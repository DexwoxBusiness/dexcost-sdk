"""Session-based auto-grouping for dexcost.

Groups related LLM and HTTP calls into a single task without
requiring explicit ``with dexcost.task():`` wrappers. Uses contextvars
for thread/async safety.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from datetime import datetime, timezone

from dexcost.context import get_context, get_current_task, set_current_task
from dexcost.models.task import Task
from dexcost.storage.protocol import StorageBackend

_log = logging.getLogger(__name__)


class SessionManager:
    """Manages auto-created session tasks for grouping cost events.

    When no explicit ``dexcost.task()`` is active, the session manager
    creates a session task and sets it in the context so that subsequent
    LLM and HTTP calls are grouped together.
    """

    def __init__(self) -> None:
        self._sessions: dict[int, Task] = {}  # context id -> task
        self._last_activity: dict[int, float] = {}  # context id -> timestamp
        self._lock = threading.Lock()

    def get_or_create_session(
        self, call_type: str, storage: StorageBackend | None = None
    ) -> Task:
        """Return the active task or create a session task.

        If an explicit task is already active in the current context,
        that task is returned unchanged. Otherwise, a session task is
        created (or reused) for the current context.

        Args:
            call_type: Description of the call (e.g. ``"llm_call"``,
                ``"http_call"``).
            storage: Optional storage backend for persisting the task.

        Returns:
            The active or newly-created session task.
        """
        ctx_id = threading.get_ident()

        # If an explicit task is already active, use it. Automatic sessions
        # also live in the task ContextVar, so distinguish the managed session
        # for this thread from an explicit task before updating activity.
        existing = get_current_task()
        if existing is not None:
            with self._lock:
                managed = self._sessions.get(ctx_id)
                if existing is managed and existing.ended_at is None:
                    self._last_activity[ctx_id] = time.monotonic()
                    return existing
                if existing is managed:
                    self._sessions.pop(ctx_id, None)
                    self._last_activity.pop(ctx_id, None)
                elif existing.ended_at is None:
                    return existing

            # An automatic session can be finalized by the background sync
            # thread, which cannot clear another thread's ContextVar. Clear
            # the stale local reference before creating the next session.
            set_current_task(None)

        # Check if we already have a session for this thread/async context
        with self._lock:
            session = self._sessions.get(ctx_id)
            if session is not None and session.ended_at is None:
                self._last_activity[ctx_id] = time.monotonic()
                # Ensure it's set as current task
                set_current_task(session)
                return session
            if session is not None:
                self._sessions.pop(ctx_id, None)
                self._last_activity.pop(ctx_id, None)

        # Create a new session task
        ctx = get_context()
        task_id = uuid.uuid4()
        has_business_identity = ctx is not None and any(
            (
                ctx.customer_id,
                ctx.project_id,
                ctx.user_id,
                ctx.product_id,
                ctx.agent,
                ctx.workflow_id,
            )
        )
        session = Task(
            task_id=task_id,
            task_type="agent_session",
            status="pending",
            started_at=datetime.now(timezone.utc),
            customer_id=ctx.customer_id if ctx else None,
            project_id=ctx.project_id if ctx else None,
            user_id=ctx.user_id if ctx else None,
            product_id=ctx.product_id if ctx else None,
            root_task_id=task_id if has_business_identity else None,
            agent_id=ctx.agent if ctx else None,
            agent_version=ctx.agent_version if ctx else None,
            workflow_id=ctx.workflow_id if ctx else None,
            workflow_session_id=ctx.workflow_session_id if ctx else None,
            metadata=dict(ctx.metadata) if ctx and ctx.metadata else {},
        )

        if storage is not None:
            try:
                storage.insert_task(session)
            except Exception:
                _log.debug("Failed to persist session task", exc_info=True)

        with self._lock:
            self._sessions[ctx_id] = session
            self._last_activity[ctx_id] = time.monotonic()

        set_current_task(session)
        return session

    def finalize_idle_sessions(
        self,
        idle_seconds: float = 30.0,
        storage: StorageBackend | None = None,
    ) -> list[Task]:
        """Finalize sessions that have had no activity for *idle_seconds*.

        When *storage* is supplied, the finalized task is persisted before it
        is removed from the manager. ``update_task`` also marks a previously
        synced running snapshot pending again, allowing the final immutable
        business identity to be delivered by the next sync batch.

        Returns:
            List of finalized session tasks.
        """
        now = time.monotonic()
        finalized: list[Task] = []

        with self._lock:
            idle_ids = [
                ctx_id
                for ctx_id, last in self._last_activity.items()
                if (now - last) >= idle_seconds
            ]
            for ctx_id in idle_ids:
                session = self._sessions.get(ctx_id)
                if session is None:
                    self._last_activity.pop(ctx_id, None)
                    continue

                previous_status = session.status
                previous_ended_at = session.ended_at
                session.status = "success"
                session.ended_at = datetime.now(timezone.utc)

                if storage is not None:
                    try:
                        storage.update_task(session)
                    except Exception:
                        # Keep the session managed and retry persistence on a
                        # later sync cycle instead of losing its final state.
                        session.status = previous_status
                        session.ended_at = previous_ended_at
                        _log.warning(
                            "Failed to persist finalized session task",
                            exc_info=True,
                        )
                        continue

                self._sessions.pop(ctx_id, None)
                self._last_activity.pop(ctx_id, None)
                finalized.append(session)

        return finalized

    def clear(self) -> None:
        """Remove all tracked sessions (for testing)."""
        with self._lock:
            self._sessions.clear()
            self._last_activity.clear()


# Module-level singleton
_session_manager: SessionManager | None = None


def get_session_manager() -> SessionManager:
    """Return the global session manager, creating it if needed."""
    global _session_manager
    if _session_manager is None:
        _session_manager = SessionManager()
    return _session_manager


def reset_session_manager() -> None:
    """Reset the global session manager (for testing)."""
    global _session_manager
    if _session_manager is not None:
        _session_manager.clear()
    _session_manager = None
