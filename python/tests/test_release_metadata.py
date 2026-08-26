from __future__ import annotations

import re
from pathlib import Path

PYPROJECT = Path(__file__).parents[1] / "pyproject.toml"


def _optional_dependency_block(pyproject: str, extra: str) -> str:
    match = re.search(
        rf"(?ms)^{re.escape(extra)} = \[\r?\n(?P<body>.*?)^\]\r?$",
        pyproject,
    )
    assert match is not None, f"missing optional dependency group {extra!r}"
    return match.group("body")


def test_python_310_all_and_crewai_extras_keep_onnxruntime_installable() -> None:
    """Guard the transitive CrewAI/ChromaDB wheel boundary in release CI."""

    pyproject = PYPROJECT.read_text(encoding="utf-8")
    requirement = '"onnxruntime>=1.14.1,<1.24; python_version < \'3.11\'"'
    assert pyproject.count(requirement) == 2


def test_gpu_extra_installs_the_nvml_binding_promised_by_the_runtime() -> None:
    """Keep ``pip install dexcost[gpu]`` functional and covered by ``all``."""

    pyproject = PYPROJECT.read_text(encoding="utf-8")
    requirement = '"nvidia-ml-py>=12.535,<14.0"'
    assert requirement in _optional_dependency_block(pyproject, "gpu")
    assert requirement in _optional_dependency_block(pyproject, "all")
