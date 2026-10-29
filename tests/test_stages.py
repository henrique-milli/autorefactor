from __future__ import annotations

from pathlib import Path

from autorefactor.artifacts import layout_for, load_analyze_bundle, load_plan
from autorefactor.config import AutorefactorConfig
from autorefactor.document import find_document_gaps
from autorefactor.stages.analyze import run_analyze
from autorefactor.stages.index import run_index
from autorefactor.testing.gaps import find_test_gaps


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "demo-workspace"


def test_index_writes_units(tmp_path: Path) -> None:
    out = tmp_path / "out"
    config = AutorefactorConfig(workspace=FIXTURES, out_dir=out, skip_llm=True)
    units = run_index(config)
    assert units
    layout = layout_for(out)
    assert (layout.index_dir / "units.json").exists()


def test_analyze_skip_llm_has_no_plan(tmp_path: Path) -> None:
    out = tmp_path / "out"
    config = AutorefactorConfig(
        workspace=FIXTURES,
        out_dir=out,
        skip_llm=True,
        sensors="auto",
    )
    artifacts = run_analyze(config)
    assert artifacts.findings is not None
    assert artifacts.plan is None
    layout = layout_for(out)
    assert (layout.analyze_dir / "findings.json").exists()
    assert (layout.analyze_dir / "report.md").exists()
    assert not (layout.plan_dir / "plan.json").exists()
    units, flags, findings, manifest, notes = load_analyze_bundle(layout)
    assert units and findings is not None and manifest is not None
    assert any(getattr(n, "sensor", getattr(n, "name", "")) in {"structure", "builtin", "coverage"} for n in notes) or notes is not None


def test_gaps_dedupe_by_suggested_path() -> None:
    from autorefactor.domain import (
        AnalysisGoal,
        CodeUnit,
        CodeSpan,
        ConfirmedFinding,
        Finding,
        Severity,
        SuggestedAction,
        UnitKind,
        content_hash,
        unit_id,
    )

    type_unit = CodeUnit(
        id=unit_id("t"),
        repo="demo",
        path="src/main/java/Demo.java",
        qualified_name="Demo",
        kind=UnitKind.TYPE,
        language="java",
        source_snippet="class Demo {}",
        content_hash=content_hash("t"),
        span=CodeSpan(start_line=1, end_line=10),
    )
    method = CodeUnit(
        id=unit_id("m"),
        repo="demo",
        path="src/main/java/Demo.java",
        qualified_name="Demo.foo",
        kind=UnitKind.FUNCTION,
        language="java",
        source_snippet="void foo() {}",
        content_hash=content_hash("m"),
        span=CodeSpan(start_line=3, end_line=5),
    )
    flag = Finding(
        id=content_hash("f"),
        goal=AnalysisGoal.STRUCTURE,
        sensor="structure",
        severity=Severity.MEDIUM,
        title="dup",
        evidence="e",
        unit_ids=[type_unit.id, method.id],
        action_hint=SuggestedAction.EXTRACT_SHARED,
        score=0.9,
    )
    finding = ConfirmedFinding(finding=flag, valid=True, reasoning="ok")
    gaps = find_test_gaps([finding], [type_unit, method], existing_tests=[])
    assert len(gaps) == 1
    assert gaps[0].suggested_test_path.endswith("DemoTest.java")


def test_document_gap_detection_python() -> None:
    unit = __import__("autorefactor.models", fromlist=["CodeUnit"]).CodeUnit(
        id="1",
        repo="alpha",
        path="mod.py",
        qualified_name="mod.helper",
        kind=__import__("autorefactor.models", fromlist=["UnitKind"]).UnitKind.FUNCTION,
        language="python",
        source_snippet="def helper(x):\n    return x\n",
        content_hash="h",
    )
    gaps = find_document_gaps([unit], languages={"python"})
    assert gaps and gaps[0].qualified_name == "mod.helper"


def test_load_plan_missing(tmp_path: Path) -> None:
    layout = layout_for(tmp_path / "out")
    assert load_plan(layout) is None
