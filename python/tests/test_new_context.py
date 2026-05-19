"""Tests for DexcostContext, set_context, get_context, clear_context."""

from __future__ import annotations

import asyncio
import threading

import pytest

from dexcost.context import (
    DexcostContext,
    clear_context,
    get_context,
    set_context,
)


# ---------------------------------------------------------------------------
# 1. set_context + get_context returns correct values
# ---------------------------------------------------------------------------

def test_set_and_get_context_basic() -> None:
    clear_context()
    set_context(customer_id="cust-001", project_id="proj-001")
    ctx = get_context()
    assert ctx is not None
    assert ctx.customer_id == "cust-001"
    assert ctx.project_id == "proj-001"
    clear_context()


# ---------------------------------------------------------------------------
# 2. get_context returns None when not set
# ---------------------------------------------------------------------------

def test_get_context_returns_none_when_not_set() -> None:
    clear_context()
    assert get_context() is None


# ---------------------------------------------------------------------------
# 3. set_context with metadata
# ---------------------------------------------------------------------------

def test_set_context_with_metadata() -> None:
    clear_context()
    set_context(customer_id="cust-002", metadata={"tier": "enterprise", "region": "us-east"})
    ctx = get_context()
    assert ctx is not None
    assert ctx.customer_id == "cust-002"
    assert ctx.metadata == {"tier": "enterprise", "region": "us-east"}
    clear_context()


# ---------------------------------------------------------------------------
# 4. set_context replaces previous context
# ---------------------------------------------------------------------------

def test_set_context_replaces_previous() -> None:
    clear_context()
    set_context(customer_id="cust-old", project_id="proj-old")
    set_context(customer_id="cust-new", project_id="proj-new")
    ctx = get_context()
    assert ctx is not None
    assert ctx.customer_id == "cust-new"
    assert ctx.project_id == "proj-new"
    clear_context()


# ---------------------------------------------------------------------------
# 5. clear_context works
# ---------------------------------------------------------------------------

def test_clear_context() -> None:
    set_context(customer_id="cust-003")
    assert get_context() is not None
    clear_context()
    assert get_context() is None


# ---------------------------------------------------------------------------
# 6. Thread safety — two threads with different customer_ids don't cross-contaminate
# ---------------------------------------------------------------------------

def test_thread_safety() -> None:
    results: dict[str, str | None] = {}
    barrier = threading.Barrier(2)

    def thread_fn(name: str, customer_id: str) -> None:
        set_context(customer_id=customer_id)
        barrier.wait()  # both threads set context before either reads
        ctx = get_context()
        results[name] = ctx.customer_id if ctx else None

    t1 = threading.Thread(target=thread_fn, args=("t1", "cust-thread-1"))
    t2 = threading.Thread(target=thread_fn, args=("t2", "cust-thread-2"))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert results["t1"] == "cust-thread-1"
    assert results["t2"] == "cust-thread-2"


# ---------------------------------------------------------------------------
# 7. Async safety — two coroutines with different customer_ids don't cross-contaminate
# ---------------------------------------------------------------------------

def test_async_safety() -> None:
    results: dict[str, str | None] = {}

    async def coro(name: str, customer_id: str) -> None:
        set_context(customer_id=customer_id)
        await asyncio.sleep(0)  # yield to allow the other coroutine to run
        ctx = get_context()
        results[name] = ctx.customer_id if ctx else None

    async def main() -> None:
        await asyncio.gather(
            coro("c1", "cust-async-1"),
            coro("c2", "cust-async-2"),
        )

    asyncio.run(main())

    assert results["c1"] == "cust-async-1"
    assert results["c2"] == "cust-async-2"
