"""Tests for configurable auto-instrumentation (US-015).

Verifies that ``CostTracker`` accepts an ``auto_instrument`` parameter to
control which SDKs are monkey-patched, that missing SDKs are silently
skipped, and that ``tracker.instrument()`` / ``tracker.uninstrument()``
work for lazy instrumentation after init.

All tests use fake SDK modules injected into ``sys.modules`` — real SDK
packages are **not** required.
"""

from __future__ import annotations

import sys
import types
from collections.abc import Generator
from typing import Any

import pytest

from dexcost.storage.sqlite import SQLiteStorage
from dexcost.tracker import ALL_SUPPORTED_INSTRUMENTS, CostTracker

# ---------------------------------------------------------------------------
# Fake SDK module installers / uninstallers
# ---------------------------------------------------------------------------


def _install_fake_openai() -> None:
    """Install a minimal fake ``openai`` package into ``sys.modules``."""
    openai_mod = types.ModuleType("openai")
    resources_mod = types.ModuleType("openai.resources")
    chat_mod = types.ModuleType("openai.resources.chat")
    completions_mod = types.ModuleType("openai.resources.chat.completions")

    class Completions:
        @staticmethod
        def create(**kwargs: Any) -> Any:
            raise NotImplementedError

    class AsyncCompletions:
        @staticmethod
        async def create(**kwargs: Any) -> Any:
            raise NotImplementedError

    completions_mod.Completions = Completions  # type: ignore[attr-defined]
    completions_mod.AsyncCompletions = AsyncCompletions  # type: ignore[attr-defined]

    chat_mod.completions = completions_mod  # type: ignore[attr-defined]
    resources_mod.chat = chat_mod  # type: ignore[attr-defined]
    openai_mod.resources = resources_mod  # type: ignore[attr-defined]

    sys.modules["openai"] = openai_mod
    sys.modules["openai.resources"] = resources_mod
    sys.modules["openai.resources.chat"] = chat_mod
    sys.modules["openai.resources.chat.completions"] = completions_mod


def _uninstall_fake_openai() -> None:
    for key in list(sys.modules):
        if key == "openai" or key.startswith("openai."):
            sys.modules[key] = None  # type: ignore[assignment]


def _install_fake_anthropic() -> None:
    """Install a minimal fake ``anthropic`` package into ``sys.modules``."""
    anthropic_mod = types.ModuleType("anthropic")
    resources_mod = types.ModuleType("anthropic.resources")
    messages_mod = types.ModuleType("anthropic.resources.messages")

    class Messages:
        @staticmethod
        def create(**kwargs: Any) -> Any:
            raise NotImplementedError

    class AsyncMessages:
        @staticmethod
        async def create(**kwargs: Any) -> Any:
            raise NotImplementedError

    messages_mod.Messages = Messages  # type: ignore[attr-defined]
    messages_mod.AsyncMessages = AsyncMessages  # type: ignore[attr-defined]

    resources_mod.messages = messages_mod  # type: ignore[attr-defined]
    anthropic_mod.resources = resources_mod  # type: ignore[attr-defined]

    sys.modules["anthropic"] = anthropic_mod
    sys.modules["anthropic.resources"] = resources_mod
    sys.modules["anthropic.resources.messages"] = messages_mod


def _uninstall_fake_anthropic() -> None:
    for key in list(sys.modules):
        if key == "anthropic" or key.startswith("anthropic."):
            sys.modules[key] = None  # type: ignore[assignment]


def _install_fake_litellm() -> None:
    """Install a minimal fake ``litellm`` package into ``sys.modules``."""
    litellm_mod = types.ModuleType("litellm")

    def completion(**kwargs: Any) -> Any:
        raise NotImplementedError

    async def acompletion(**kwargs: Any) -> Any:
        raise NotImplementedError

    litellm_mod.completion = completion  # type: ignore[attr-defined]
    litellm_mod.acompletion = acompletion  # type: ignore[attr-defined]

    sys.modules["litellm"] = litellm_mod


def _uninstall_fake_litellm() -> None:
    for key in list(sys.modules):
        if key == "litellm" or key.startswith("litellm."):
            sys.modules[key] = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Helpers for resetting global instrument state between tests
# ---------------------------------------------------------------------------


def _reset_instrument_state() -> None:
    """Reset the global ``_patched`` flags in all instrument modules."""
    from dexcost.instruments.anthropic import uninstrument_anthropic
    from dexcost.instruments.litellm import uninstrument_litellm
    from dexcost.instruments.openai import uninstrument_openai

    uninstrument_openai()
    uninstrument_anthropic()
    uninstrument_litellm()

    # Also reset instruments added after the original three
    try:
        from dexcost.instruments.bedrock import uninstrument_bedrock
        uninstrument_bedrock()
    except ImportError:
        pass
    try:
        from dexcost.instruments.gemini import uninstrument_gemini
        uninstrument_gemini()
    except ImportError:
        pass
    try:
        from dexcost.instruments.cohere import uninstrument_cohere
        uninstrument_cohere()
    except ImportError:
        pass
    try:
        from dexcost.instruments.mcp import uninstrument_mcp
        uninstrument_mcp()
    except ImportError:
        pass


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def storage(tmp_path: Any) -> Generator[SQLiteStorage, None, None]:
    """Create a fresh SQLite storage for each test."""
    s = SQLiteStorage(db_path=tmp_path / "test.db")
    yield s
    s.close()


@pytest.fixture(autouse=True)
def _install_all_fakes() -> Generator[None, None, None]:
    """Install all fake SDKs before each test and clean up after."""
    _install_fake_openai()
    _install_fake_anthropic()
    _install_fake_litellm()
    yield
    _reset_instrument_state()
    _uninstall_fake_openai()
    _uninstall_fake_anthropic()
    _uninstall_fake_litellm()


# ---------------------------------------------------------------------------
# AC1: CostTracker(auto_instrument=["openai", "anthropic"]) only patches those two
# ---------------------------------------------------------------------------


class TestSelectiveInstrumentation:
    """Only the SDKs listed in auto_instrument are patched."""

    def test_only_openai_and_anthropic(self, storage: SQLiteStorage) -> None:
        tracker = CostTracker(
            storage=storage,
            auto_instrument=["openai", "anthropic"],
            auto_update_pricing=False,
        )

        assert "openai" in tracker.instrumented
        assert "anthropic" in tracker.instrumented
        assert "litellm" not in tracker.instrumented

    def test_only_openai(self, storage: SQLiteStorage) -> None:
        tracker = CostTracker(
            storage=storage,
            auto_instrument=["openai"],
            auto_update_pricing=False,
        )

        assert "openai" in tracker.instrumented
        assert "anthropic" not in tracker.instrumented
        assert "litellm" not in tracker.instrumented

    def test_only_litellm(self, storage: SQLiteStorage) -> None:
        tracker = CostTracker(
            storage=storage,
            auto_instrument=["litellm"],
            auto_update_pricing=False,
        )

        assert "litellm" in tracker.instrumented
        assert "openai" not in tracker.instrumented
        assert "anthropic" not in tracker.instrumented


# ---------------------------------------------------------------------------
# AC2: CostTracker(auto_instrument=[]) disables all patching
# ---------------------------------------------------------------------------


class TestDisableAllPatching:
    """auto_instrument=[] disables all patching entirely."""

    def test_empty_list_no_patching(self, storage: SQLiteStorage) -> None:
        tracker = CostTracker(
            storage=storage,
            auto_instrument=[],
            auto_update_pricing=False,
        )

        assert len(tracker.instrumented) == 0
        assert "openai" not in tracker.instrumented
        assert "anthropic" not in tracker.instrumented
        assert "litellm" not in tracker.instrumented


# ---------------------------------------------------------------------------
# AC3: Default patches all supported
# ---------------------------------------------------------------------------


class TestDefaultPatchesAll:
    """Default: auto_instrument=None patches all supported SDKs."""

    def test_default_patches_all_installed(self, storage: SQLiteStorage) -> None:
        tracker = CostTracker(
            storage=storage,
            auto_update_pricing=False,
        )

        assert "openai" in tracker.instrumented
        assert "anthropic" in tracker.instrumented
        assert "litellm" in tracker.instrumented

        # All installed SDKs should be instrumented; uninstalled ones are skipped.
        # We also catch Exception (not just ImportError) because native
        # extensions can raise non-ImportError failures (e.g. pyo3 panics
        # when cffi is missing).
        expected: set[str] = set()
        for name in ALL_SUPPORTED_INSTRUMENTS:
            try:
                if name == "openai":
                    import openai as _  # noqa: F401
                elif name == "anthropic":
                    import anthropic as _  # noqa: F401
                elif name == "litellm":
                    import litellm as _  # noqa: F401
                elif name == "gemini":
                    import google.genai as _  # noqa: F401
                elif name == "bedrock":
                    import botocore as _  # noqa: F401
                elif name == "cohere":
                    import cohere as _  # noqa: F401
                elif name == "mcp":
                    import mcp as _  # noqa: F401
                expected.add(name)
            except (KeyboardInterrupt, SystemExit):
                raise
            except BaseException:
                pass
        assert tracker.instrumented == frozenset(expected)


# ---------------------------------------------------------------------------
# AC4: Patching only applies if SDK is actually installed
# ---------------------------------------------------------------------------


class TestMissingSDKSkipped:
    """No ImportError if an SDK is not installed — it is silently skipped."""

    def test_missing_openai_skipped(self, storage: SQLiteStorage) -> None:
        """If openai is not installed, auto_instrument silently skips it."""
        _uninstall_fake_openai()

        tracker = CostTracker(
            storage=storage,
            auto_instrument=["openai", "anthropic"],
            auto_update_pricing=False,
        )

        assert "openai" not in tracker.instrumented
        assert "anthropic" in tracker.instrumented

        # Re-install for cleanup
        _install_fake_openai()

    def test_missing_anthropic_skipped(self, storage: SQLiteStorage) -> None:
        _uninstall_fake_anthropic()

        tracker = CostTracker(
            storage=storage,
            auto_instrument=["openai", "anthropic"],
            auto_update_pricing=False,
        )

        assert "openai" in tracker.instrumented
        assert "anthropic" not in tracker.instrumented

        _install_fake_anthropic()

    def test_missing_litellm_skipped(self, storage: SQLiteStorage) -> None:
        _uninstall_fake_litellm()

        tracker = CostTracker(
            storage=storage,
            auto_instrument=["litellm"],
            auto_update_pricing=False,
        )

        assert "litellm" not in tracker.instrumented

        _install_fake_litellm()

    def test_all_missing_returns_empty(self, storage: SQLiteStorage) -> None:
        """When requested SDKs are not installed, auto_instrument skips them."""
        _uninstall_fake_openai()
        _uninstall_fake_anthropic()
        _uninstall_fake_litellm()

        tracker = CostTracker(
            storage=storage,
            auto_instrument=["openai", "anthropic", "litellm"],
            auto_update_pricing=False,
        )

        assert len(tracker.instrumented) == 0

        _install_fake_openai()
        _install_fake_anthropic()
        _install_fake_litellm()


# ---------------------------------------------------------------------------
# AC5: tracker.instrument("openai") for lazy instrumentation
# ---------------------------------------------------------------------------


class TestLazyInstrumentation:
    """tracker.instrument(name) can be called manually after init."""

    def test_lazy_instrument_openai(self, storage: SQLiteStorage) -> None:
        tracker = CostTracker(
            storage=storage,
            auto_instrument=[],
            auto_update_pricing=False,
        )

        assert "openai" not in tracker.instrumented

        tracker.instrument("openai")

        assert "openai" in tracker.instrumented

    def test_lazy_instrument_anthropic(self, storage: SQLiteStorage) -> None:
        tracker = CostTracker(
            storage=storage,
            auto_instrument=[],
            auto_update_pricing=False,
        )

        tracker.instrument("anthropic")

        assert "anthropic" in tracker.instrumented

    def test_lazy_instrument_litellm(self, storage: SQLiteStorage) -> None:
        tracker = CostTracker(
            storage=storage,
            auto_instrument=[],
            auto_update_pricing=False,
        )

        tracker.instrument("litellm")

        assert "litellm" in tracker.instrumented

    def test_lazy_instrument_missing_sdk_raises(self, storage: SQLiteStorage) -> None:
        """Manual instrument() raises ImportError if SDK not installed."""
        _uninstall_fake_openai()

        tracker = CostTracker(
            storage=storage,
            auto_instrument=[],
            auto_update_pricing=False,
        )

        with pytest.raises(ImportError, match="openai"):
            tracker.instrument("openai")

        _install_fake_openai()

    def test_instrument_invalid_name_raises(self, storage: SQLiteStorage) -> None:
        tracker = CostTracker(
            storage=storage,
            auto_instrument=[],
            auto_update_pricing=False,
        )

        with pytest.raises(ValueError, match="Unsupported"):
            tracker.instrument("invalid_sdk")

    def test_double_instrument_raises(self, storage: SQLiteStorage) -> None:
        tracker = CostTracker(
            storage=storage,
            auto_instrument=["openai"],
            auto_update_pricing=False,
        )

        with pytest.raises(RuntimeError, match="already instrumented"):
            tracker.instrument("openai")


# ---------------------------------------------------------------------------
# Uninstrument
# ---------------------------------------------------------------------------


class TestUninstrument:
    """tracker.uninstrument(name) removes patching."""

    def test_uninstrument_openai(self, storage: SQLiteStorage) -> None:
        tracker = CostTracker(
            storage=storage,
            auto_instrument=["openai"],
            auto_update_pricing=False,
        )
        assert "openai" in tracker.instrumented

        tracker.uninstrument("openai")

        assert "openai" not in tracker.instrumented

    def test_uninstrument_not_instrumented_is_noop(self, storage: SQLiteStorage) -> None:
        tracker = CostTracker(
            storage=storage,
            auto_instrument=[],
            auto_update_pricing=False,
        )

        # Should not raise
        tracker.uninstrument("openai")
        assert "openai" not in tracker.instrumented

    def test_uninstrument_invalid_name_raises(self, storage: SQLiteStorage) -> None:
        tracker = CostTracker(
            storage=storage,
            auto_instrument=[],
            auto_update_pricing=False,
        )

        with pytest.raises(ValueError, match="Unsupported"):
            tracker.uninstrument("invalid_sdk")

    def test_instrument_after_uninstrument(self, storage: SQLiteStorage) -> None:
        """Can re-instrument after uninstrumenting."""
        tracker = CostTracker(
            storage=storage,
            auto_instrument=["openai"],
            auto_update_pricing=False,
        )

        tracker.uninstrument("openai")
        assert "openai" not in tracker.instrumented

        tracker.instrument("openai")
        assert "openai" in tracker.instrumented


# ---------------------------------------------------------------------------
# AC6: Verify only specified SDKs are patched (integration-level)
# ---------------------------------------------------------------------------


class TestPatchingVerification:
    """Verify that the underlying instrument modules are actually called."""

    def test_openai_patched_after_auto_instrument(self, storage: SQLiteStorage) -> None:
        """When openai is in auto_instrument, the openai module is actually patched."""
        from dexcost.instruments import openai as openai_instrument

        assert not openai_instrument._patched

        CostTracker(
            storage=storage,
            auto_instrument=["openai"],
            auto_update_pricing=False,
        )

        assert openai_instrument._patched

    def test_anthropic_patched_after_auto_instrument(self, storage: SQLiteStorage) -> None:
        from dexcost.instruments import anthropic as anthropic_instrument

        assert not anthropic_instrument._patched

        CostTracker(
            storage=storage,
            auto_instrument=["anthropic"],
            auto_update_pricing=False,
        )

        assert anthropic_instrument._patched

    def test_litellm_patched_after_auto_instrument(self, storage: SQLiteStorage) -> None:
        from dexcost.instruments import litellm as litellm_instrument

        assert not litellm_instrument._patched

        CostTracker(
            storage=storage,
            auto_instrument=["litellm"],
            auto_update_pricing=False,
        )

        assert litellm_instrument._patched

    def test_openai_not_patched_when_excluded(self, storage: SQLiteStorage) -> None:
        """When openai is NOT in auto_instrument, openai module stays unpatched."""
        from dexcost.instruments import openai as openai_instrument

        assert not openai_instrument._patched

        CostTracker(
            storage=storage,
            auto_instrument=["anthropic", "litellm"],
            auto_update_pricing=False,
        )

        assert not openai_instrument._patched

    def test_litellm_not_patched_when_excluded(self, storage: SQLiteStorage) -> None:
        from dexcost.instruments import litellm as litellm_instrument

        assert not litellm_instrument._patched

        CostTracker(
            storage=storage,
            auto_instrument=["openai", "anthropic"],
            auto_update_pricing=False,
        )

        assert not litellm_instrument._patched

    def test_nothing_patched_with_empty_list(self, storage: SQLiteStorage) -> None:
        from dexcost.instruments import anthropic as anthropic_instrument
        from dexcost.instruments import litellm as litellm_instrument
        from dexcost.instruments import openai as openai_instrument

        CostTracker(
            storage=storage,
            auto_instrument=[],
            auto_update_pricing=False,
        )

        assert not openai_instrument._patched
        assert not anthropic_instrument._patched
        assert not litellm_instrument._patched


# ---------------------------------------------------------------------------
# Public API exports
# ---------------------------------------------------------------------------


class TestPublicAPI:
    """ALL_SUPPORTED_INSTRUMENTS and new methods are accessible."""

    def test_all_supported_instruments_exported(self) -> None:
        import dexcost

        assert hasattr(dexcost, "ALL_SUPPORTED_INSTRUMENTS")
        assert dexcost.ALL_SUPPORTED_INSTRUMENTS == [
            "openai",
            "anthropic",
            "litellm",
            "gemini",
            "bedrock",
            "cohere",
            "mcp",
        ]

    def test_tracker_has_instrument_method(self, storage: SQLiteStorage) -> None:
        tracker = CostTracker(
            storage=storage,
            auto_instrument=[],
            auto_update_pricing=False,
        )
        assert hasattr(tracker, "instrument")
        assert callable(tracker.instrument)

    def test_tracker_has_uninstrument_method(self, storage: SQLiteStorage) -> None:
        tracker = CostTracker(
            storage=storage,
            auto_instrument=[],
            auto_update_pricing=False,
        )
        assert hasattr(tracker, "uninstrument")
        assert callable(tracker.uninstrument)

    def test_tracker_has_instrumented_property(self, storage: SQLiteStorage) -> None:
        tracker = CostTracker(
            storage=storage,
            auto_instrument=[],
            auto_update_pricing=False,
        )
        assert hasattr(tracker, "instrumented")
        assert isinstance(tracker.instrumented, frozenset)
