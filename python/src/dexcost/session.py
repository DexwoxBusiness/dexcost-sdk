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
from typing import Any

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
        # If an explicit task is already active, use it
        existing = get_current_task()
        if existing is not None:
            ctx_id = id(existing)
            with self._lock:
                self._last_activity[ctx_id] = time.monotonic()
            return existing

        # Check if we already have a session for this thread/async context
        ctx_id = threading.get_ident()
        with self._lock:
            session = self._sessions.get(ctx_id)
            if session is not None:
                self._last_activity[ctx_id] = time.monotonic()
                # Ensure it's set as current task
                set_current_task(session)
                return session

        # Create a new session task
        ctx = get_context()
        agent = getattr(ctx, "agent", None) if ctx else None
        task_type = agent if agent else "agent_session"

        session = Task(
            task_id=uuid.uuid4(),
            task_type=task_type,
            status="pending",
            started_at=datetime.now(timezone.utc),
            customer_id=ctx.customer_id if ctx else None,
            project_id=ctx.project_id if ctx else None,
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

    def finalize_idle_sessions(self, idle_seconds: float = 30.0) -> list[Task]:
        """Finalize sessions that have had no activity for *idle_seconds*.

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
                session = self._sessions.pop(ctx_id, None)
                self._last_activity.pop(ctx_id, None)
                if session is not None:
                    session.status = "success"
                    session.ended_at = datetime.now(timezone.utc)
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
