"""Context-scoped flag that suppresses the per-call network event."""

import asyncio

import pytest

from dexcost.context import is_network_event_suppressed, suppress_network_event


def test_default_not_suppressed():
    assert is_network_event_suppressed() is False


def test_suppress_context_manager_sets_and_clears():
    assert is_network_event_suppressed() is False
    with suppress_network_event():
        assert is_network_event_suppressed() is True
    assert is_network_event_suppressed() is False


def test_nested_suppression_restores_outer_state():
    with suppress_network_event():
        with suppress_network_event():
            assert is_network_event_suppressed() is True
        assert is_network_event_suppressed() is True
    assert is_network_event_suppressed() is False


def test_suppression_does_not_leak_after_exception():
    """The try/finally reset fires even when an exception escapes the block."""
    with pytest.raises(RuntimeError):
        with suppress_network_event():
            raise RuntimeError("boom")
    assert is_network_event_suppressed() is False


def test_suppression_propagates_into_async_child_task():
    """contextvars copy into child asyncio tasks, so the flag is visible there."""

    async def _run() -> None:
        child_saw: list[bool] = []

        async def child() -> None:
            child_saw.append(is_network_event_suppressed())
            await asyncio.sleep(0)  # yield to event loop; keeps this a proper coroutine

        with suppress_network_event():
            # create_task copies the current context; await before exiting block.
            task = asyncio.create_task(child())
            await task
            assert child_saw[0] is True  # child inherited the suppressed flag
        assert is_network_event_suppressed() is False  # parent restored after block

    asyncio.run(_run())
