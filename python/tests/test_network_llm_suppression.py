"""An LLM call must not also produce a standalone network event."""


def test_llm_instrument_wraps_call_in_suppression():
    """Each instrument module references suppress_network_event so a
    future edit that drops it fails CI.

    The behavioural guarantee is exercised by test_network_capture.py
    (test_suppressed_call_records_bytes_but_no_network_event).
    """
    import inspect

    from dexcost.instruments import (
        anthropic, bedrock, cohere, gemini, litellm, mcp, openai,
    )

    for module in (openai, anthropic, bedrock, gemini, cohere, litellm, mcp):
        src = inspect.getsource(module)
        assert "suppress_network_event" in src, (
            f"{module.__name__} must wrap its HTTP call in suppress_network_event()"
        )
