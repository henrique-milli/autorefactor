from __future__ import annotations

from pathlib import Path

from autorefactor.config import CORE_SENSORS, FULL_SENSORS, AutorefactorConfig
from autorefactor.domain import CodeSpan, CodeUnit, UnitKind, content_hash, unit_id
from autorefactor.sensors.base import AnalysisContext
from autorefactor.sensors.complexity import ComplexitySensor, measure_complexity
from autorefactor.sensors.discovery import run_sensors


def test_measure_complexity_nested_python() -> None:
    src = '''
def messy(x):
    if x:
        if x > 1:
            if x > 2:
                if x > 3:
                    for i in range(x):
                        if i % 2:
                            return i
    return 0
'''
    nesting, cx = measure_complexity(src, "python")
    assert nesting >= 4
    assert cx >= 5


def test_complexity_sensor_flags_nested(tmp_path: Path) -> None:
    # Complexity sensor requires min ~40 LOC before emitting.
    padding = "\n".join(f"    step_{i} = {i}" for i in range(30))
    snippet = f'''
def messy(x):
    if x:
        if x > 1:
            if x > 2:
                if x > 3:
                    for i in range(x):
                        if i % 2:
                            return i
{padding}
    return 0
'''
    unit = CodeUnit(
        id=unit_id("fn"),
        repo="demo",
        path="src/messy.py",
        qualified_name="messy",
        kind=UnitKind.FUNCTION,
        language="python",
        source_snippet=snippet,
        content_hash=content_hash("fn"),
        span=CodeSpan(start_line=1, end_line=snippet.count("\n") + 1),
    )
    shallow = CodeUnit(
        id=unit_id("fn2"),
        repo="demo",
        path="src/ok.py",
        qualified_name="ok",
        kind=UnitKind.FUNCTION,
        language="python",
        source_snippet="def ok():\n    return 1\n",
        content_hash=content_hash("fn2"),
        span=CodeSpan(start_line=1, end_line=2),
    )
    ctx = AnalysisContext(
        workspace=tmp_path,
        repos=(tmp_path,),
        units=(unit, shallow),
        config=AutorefactorConfig(workspace=tmp_path, out_dir=tmp_path / "out"),
        focus="balanced",
    )
    findings = ComplexitySensor().collect(ctx)
    assert any("messy" in f.title for f in findings)
    assert not any("ok" in f.title for f in findings)


def test_core_sensors_default_excludes_static() -> None:
    cfg = AutorefactorConfig()
    assert "static.semgrep" not in cfg.enabled_sensors()
    assert "complexity" in cfg.enabled_sensors()
    assert cfg.enabled_sensors() == CORE_SENSORS


def test_full_profile_includes_static(monkeypatch) -> None:
    monkeypatch.setenv("AUTOREFACTOR_PROFILE", "full")
    cfg = AutorefactorConfig()
    assert cfg.enabled_sensors() == FULL_SENSORS
    assert "static.semgrep" in cfg.enabled_sensors()


def test_requested_static_missing_is_error(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PATH", "/nonexistent")
    cfg = AutorefactorConfig(
        workspace=tmp_path,
        out_dir=tmp_path / "out",
        sensors="structure,static.semgrep",
        skip_llm=True,
    )
    ctx = AnalysisContext(
        workspace=tmp_path,
        repos=(tmp_path,),
        units=(),
        config=cfg,
        focus="balanced",
    )
    _, reports = run_sensors(ctx, concurrency=2)
    by = {r.sensor: r for r in reports}
    assert by["static.semgrep"].status == "error"
    assert "not available" in (by["static.semgrep"].detail or "").lower()
