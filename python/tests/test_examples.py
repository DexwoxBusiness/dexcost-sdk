"""Smoke tests for example scripts (US-020).

Validate that each example script can be imported and executed
without errors, using tmp_path for database files.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

_EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"
_SRC_DIR = Path(__file__).resolve().parent.parent / "src"

# Ensure subprocesses can find the local dexcost package even when it is not
# installed in the interpreter that the test runner uses.
_SUBPROCESS_ENV = {**os.environ, "PYTHONPATH": str(_SRC_DIR)}


class TestExamplesImport:
    """Each example should be importable without side effects at the module level."""

    def test_quickstart_importable(self) -> None:
        """quickstart.py is a valid Python file."""
        path = _EXAMPLES_DIR / "quickstart.py"
        assert path.exists(), f"Missing {path}"
        code = path.read_text(encoding="utf-8")
        compile(code, str(path), "exec")

    def test_customer_attribution_importable(self) -> None:
        """customer_attribution.py is a valid Python file."""
        path = _EXAMPLES_DIR / "customer_attribution.py"
        assert path.exists(), f"Missing {path}"
        code = path.read_text(encoding="utf-8")
        compile(code, str(path), "exec")

    def test_waste_detection_importable(self) -> None:
        """waste_detection.py is a valid Python file."""
        path = _EXAMPLES_DIR / "waste_detection.py"
        assert path.exists(), f"Missing {path}"
        code = path.read_text(encoding="utf-8")
        compile(code, str(path), "exec")


class TestExamplesRun:
    """Each example should run to completion in a subprocess."""

    def test_quickstart_runs(self, tmp_path: Any) -> None:
        """quickstart.py runs without error and produces output."""
        result = subprocess.run(
            [sys.executable, str(_EXAMPLES_DIR / "quickstart.py")],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(tmp_path),
            env=_SUBPROCESS_ENV,
        )
        assert result.returncode == 0, f"quickstart.py failed:\n{result.stderr}"
        assert "Total cost:" in result.stdout
        assert "LLM cost:" in result.stdout

    def test_customer_attribution_runs(self, tmp_path: Any) -> None:
        """customer_attribution.py runs without error."""
        result = subprocess.run(
            [sys.executable, str(_EXAMPLES_DIR / "customer_attribution.py")],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(tmp_path),
            env=_SUBPROCESS_ENV,
        )
        assert result.returncode == 0, f"customer_attribution.py failed:\n{result.stderr}"
        assert "acme-corp" in result.stdout
        assert "globex-inc" in result.stdout

    def test_waste_detection_runs(self, tmp_path: Any) -> None:
        """waste_detection.py runs without error."""
        result = subprocess.run(
            [sys.executable, str(_EXAMPLES_DIR / "waste_detection.py")],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(tmp_path),
            env=_SUBPROCESS_ENV,
        )
        assert result.returncode == 0, f"waste_detection.py failed:\n{result.stderr}"
        assert "Retry count:" in result.stdout
        assert "Retry waste:" in result.stdout
        assert "Waste ratio:" in result.stdout
