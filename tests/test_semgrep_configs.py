from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from autorefactor.sensors.static.semgrep import SemgrepAnalyzer


def test_semgrep_defaults_include_java() -> None:
    units = [SimpleNamespace(language="java")]
    ctx = SimpleNamespace(
        units=units,
        config=SimpleNamespace(semgrep_config=None),
        workspace=Path("."),
        repos=[],
    )
    configs = SemgrepAnalyzer()._configs_for(ctx)
    assert "p/ci" in configs
    assert "p/java" in configs


def test_semgrep_explicit_config_wins() -> None:
    units = [SimpleNamespace(language="java")]
    ctx = SimpleNamespace(
        units=units,
        config=SimpleNamespace(semgrep_config="p/security-audit"),
        workspace=Path("."),
        repos=[],
    )
    assert SemgrepAnalyzer()._configs_for(ctx) == ["p/security-audit"]
