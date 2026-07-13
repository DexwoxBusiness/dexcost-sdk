"""Tests for debug mode (parity with TS core/debug.ts).

Enabled via init(debug=True) / set_debug_mode() or DEXCOST_DEBUG env var.
Output goes to stderr, prefixed [dexcost:<scope>], and is a strict no-op
when disabled.
"""

from __future__ import annotations

from typing import Any

import pytest

from dexcost.debug import (
    _reset_debug_mode_for_tests,
    debug_log,
    is_debug_mode,
    set_debug_mode,
)


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch: Any) -> Any:
    monkeypatch.delenv("DEXCOST_DEBUG", raising=False)
    _reset_debug_mode_for_tests()
    yield
    _reset_debug_mode_for_tests()


class TestDebugToggle:
    def test_disabled_by_default(self) -> None:
        assert not is_debug_mode()

    def test_programmatic_enable(self) -> None:
        set_debug_mode(True)
        assert is_debug_mode()

    def test_override_wins_over_env(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("DEXCOST_DEBUG", "1")
        set_debug_mode(False)
        assert not is_debug_mode()

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
    def test_env_truthy_values(self, monkeypatch: Any, value: str) -> None:
        monkeypatch.setenv("DEXCOST_DEBUG", value)
        assert is_debug_mode()

    @pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
    def test_env_falsy_values(self, monkeypatch: Any, value: str) -> None:
        monkeypatch.setenv("DEXCOST_DEBUG", value)
        assert not is_debug_mode()


class TestDebugLog:
    def test_noop_when_disabled(self, capsys: Any) -> None:
        debug_log("http", "should not appear")
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""

    def test_writes_scoped_line_to_stderr(self, capsys: Any) -> None:
        set_debug_mode(True)
        debug_log("http", "llm_call captured via fallback")
        captured = capsys.readouterr()
        assert captured.out == ""  # never pollutes stdout
        assert "[dexcost:http] llm_call captured via fallback" in captured.err
