from __future__ import annotations

from pathlib import Path

from autorefactor.config import AutorefactorConfig
from autorefactor.sensors.base import AnalysisContext
from autorefactor.sensors.test_quality import TestQualitySensor
from autorefactor.stages.index import run_index

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "demo-workspace"


def test_test_quality_flags_demo_alpha_stubs(tmp_path: Path) -> None:
    units = run_index(AutorefactorConfig(workspace=FIXTURES, out_dir=tmp_path, skip_llm=True))
    ctx = AnalysisContext(
        workspace=FIXTURES,
        repos=tuple(p for p in FIXTURES.iterdir() if p.is_dir()),
        units=tuple(units),
        config=AutorefactorConfig(workspace=FIXTURES, skip_llm=True),
        focus="balanced",
    )
    findings = TestQualitySensor().collect(ctx)
    assert findings
    assert any(f.sensor == "test_quality" and f.severity.value == "high" for f in findings)
    shallow = [f for f in findings if f.metadata.get("kind") == "symbol_exists_only"]
    boiler = [f for f in findings if f.metadata.get("kind") == "duplicate_boilerplate"]
    assert shallow or boiler
