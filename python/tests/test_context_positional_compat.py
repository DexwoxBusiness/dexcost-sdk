"""``set_context`` and the task APIs keep their published positional order.

Adding a parameter in the *middle* of a signature is a silent breaking change
in Python: every existing positional call keeps working and starts meaning
something else. ``set_context("acme", "billing", {}, "triage-bot", "v1")`` was
a valid call before ``user_id``/``product_id`` existed; if those two land in
front of ``agent``, the same call quietly stores the agent name as a user id
and drops the agent identity on the floor — no error, just wrong attribution
for everyone who upgrades.

So the two new business-identity fields are keyword-only. These tests pin that:
the historical positional forms still mean what they always meant, and the new
fields cannot be passed positionally at all.
"""

from __future__ import annotations

import inspect

import pytest

import dexcost
from dexcost.context import clear_context, get_context, set_context


@pytest.fixture(autouse=True)
def _clean_context():
    clear_context()
    yield
    clear_context()


# The positional order published before user_id/product_id were added.
LEGACY_POSITIONAL = (
    "customer_id",
    "project_id",
    "metadata",
    "agent",
    "agent_version",
    "workflow_id",
    "workflow_session_id",
)


class TestSetContextPositionalCompatibility:
    def test_legacy_positional_call_keeps_agent_identity(self) -> None:
        """The five-argument form from before 0.18 still means what it meant."""
        set_context("acme", "billing", {"tier": "gold"}, "triage-bot", "v1")

        ctx = get_context()
        assert ctx is not None
        assert ctx.customer_id == "acme"
        assert ctx.project_id == "billing"
        assert ctx.metadata == {"tier": "gold"}
        assert ctx.agent == "triage-bot"
        assert ctx.agent_version == "v1"
        # The new fields were not supplied, so they stay unset — rather than
        # swallowing the agent identity.
        assert ctx.user_id is None
        assert ctx.product_id is None

    def test_full_legacy_positional_call_is_unshifted(self) -> None:
        set_context("acme", "billing", {}, "triage-bot", "v1", "wf-7", "sess-3")

        ctx = get_context()
        assert ctx is not None
        assert (ctx.agent, ctx.agent_version) == ("triage-bot", "v1")
        assert (ctx.workflow_id, ctx.workflow_session_id) == ("wf-7", "sess-3")

    def test_new_identity_fields_are_keyword_only(self) -> None:
        """They cannot be passed positionally, now or in any future ordering."""
        signature = inspect.signature(set_context)
        positional = [
            name
            for name, p in signature.parameters.items()
            if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
        ]
        assert tuple(positional) == LEGACY_POSITIONAL
        for name in ("user_id", "product_id"):
            assert signature.parameters[name].kind is inspect.Parameter.KEYWORD_ONLY

    def test_new_identity_fields_still_work_by_keyword(self) -> None:
        set_context(customer_id="acme", user_id="u-42", product_id="assistant")

        ctx = get_context()
        assert ctx is not None
        assert ctx.user_id == "u-42"
        assert ctx.product_id == "assistant"

    def test_top_level_export_has_the_same_signature(self) -> None:
        """``dexcost.set_context`` is the documented entry point — pin it too."""
        signature = inspect.signature(dexcost.set_context)
        positional = [
            name
            for name, p in signature.parameters.items()
            if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
        ]
        assert tuple(positional) == LEGACY_POSITIONAL


class TestTaskApiPositionalCompatibility:
    """``track_gpu`` was positional parameter 15 and must not have moved."""

    LEGACY_TAIL = ("workflow_id", "workflow_session_id", "track_gpu")

    @pytest.mark.parametrize("method_name", ["start_task", "task", "track_task"])
    def test_track_gpu_keeps_its_position(self, method_name: str) -> None:
        from dexcost.tracker import CostTracker

        signature = inspect.signature(getattr(CostTracker, method_name))
        positional = [
            name
            for name, p in signature.parameters.items()
            if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD) and name != "self"
        ]
        assert tuple(positional[-3:]) == self.LEGACY_TAIL
        for name in ("user_id", "product_id"):
            assert signature.parameters[name].kind is inspect.Parameter.KEYWORD_ONLY
