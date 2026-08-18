from __future__ import annotations

import json
from pathlib import Path


def assert_queue_quality(out_dir: Path) -> None:
    queue_path = out_dir / "queue" / "changesets.json"
    compat = out_dir / "changesets" / "queue.json"
    metrics_path = out_dir / "report" / "metrics.json"
    agenda_path = out_dir / "agenda" / "agenda.json"
    coverage_path = out_dir / "index" / "coverage.json"
    if not coverage_path.exists():
        coverage_path = out_dir / "enrich" / "coverage.json"
    assert queue_path.exists() or compat.exists()
    assert metrics_path.exists()
    assert agenda_path.exists()
    assert coverage_path.exists()

    path = queue_path if queue_path.exists() else compat
    changesets = json.loads(path.read_text(encoding="utf-8")).get("changesets") or []
    metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
    empty = [c for c in changesets if not c.get("targets")]
    assert not empty
    exact = metrics.get("exact") or {}
    assert exact.get("changesets_with_targets", 0) == len(changesets)


def test_demo_workspace_queue_quality(tmp_path: Path) -> None:
    from autorefactor.config import AutorefactorConfig
    from autorefactor.engine import run_analyze

    fixtures = Path(__file__).resolve().parents[1] / "fixtures" / "demo-workspace"
    out = tmp_path / "out"
    config = AutorefactorConfig(
        workspace=fixtures,
        out_dir=out,
        skip_llm=True,
        sensors="structure,coverage",
        focus="balanced",
    )
    run_analyze(config)
    assert_queue_quality(out)


def test_missing_coverage_queued(tmp_path: Path) -> None:
    from autorefactor.config import AutorefactorConfig
    from autorefactor.engine import run_analyze

    fixtures = Path(__file__).resolve().parents[1] / "fixtures" / "demo-workspace"
    out = tmp_path / "out-cov"
    artifacts = run_analyze(
        AutorefactorConfig(
            workspace=fixtures,
            out_dir=out,
            skip_llm=True,
            sensors="structure,coverage",
            focus="balanced",
        )
    )
    assert artifacts.coverage is not None
    assert artifacts.coverage.source == "missing"
    queue = json.loads((out / "queue" / "changesets.json").read_text(encoding="utf-8"))
    ensure = [c for c in queue["changesets"] if c.get("action") == "ensure_tests"]
    assert ensure
    assert all(c.get("targets") for c in ensure)


def test_no_artificial_queue_cap() -> None:
    from autorefactor.domain import (
        AnalysisGoal,
        CodeSpan,
        CodeUnit,
        Finding,
        Severity,
        SuggestedAction,
        UnitKind,
        content_hash,
        unit_id,
    )
    from autorefactor.plan import plan_findings
    from autorefactor.providers import StubProvider

    units = []
    findings = []
    for i in range(25):
        uid = unit_id(f"u{i}")
        units.append(
            CodeUnit(
                id=uid,
                repo="demo",
                path=f"src/F{i}.java",
                qualified_name=f"F{i}.longMethod",
                kind=UnitKind.FUNCTION,
                language="java",
                source_snippet="void longMethod() {\n" + ("  x++;\n" * 60) + "}\n",
                content_hash=content_hash(f"u{i}"),
                span=CodeSpan(start_line=1, end_line=62),
            )
        )
        findings.append(
            Finding(
                id=f"flag-{i}",
                goal=AnalysisGoal.STRUCTURE,
                sensor="structure",
                severity=Severity.MEDIUM,
                title=f"Long method {i}",
                evidence="too long",
                unit_ids=[uid],
                action_hint=SuggestedAction.SPLIT,
                score=0.8,
            )
        )
    _c, changesets, decisions, _agenda = plan_findings(findings, units, StubProvider())
    assert len(changesets) == 25
    assert len([d for d in decisions if d.valid]) == 25


def test_move_can_survive_planner() -> None:
    from autorefactor.domain import (
        AnalysisGoal,
        CodeSpan,
        CodeUnit,
        Finding,
        Severity,
        SuggestedAction,
        UnitKind,
        content_hash,
        unit_id,
    )
    from autorefactor.plan import plan_findings
    from autorefactor.providers import StubProvider

    a = CodeUnit(
        id=unit_id("fa"),
        repo="demo",
        path="A.java",
        qualified_name="A.foo",
        kind=UnitKind.FUNCTION,
        language="java",
        source_snippet="void foo() { return; }",
        content_hash=content_hash("fa"),
        span=CodeSpan(start_line=1, end_line=3),
    )
    b = CodeUnit(
        id=unit_id("tb"),
        repo="demo",
        path="B.java",
        qualified_name="B",
        kind=UnitKind.TYPE,
        language="java",
        source_snippet="class B {}",
        content_hash=content_hash("tb"),
        span=CodeSpan(start_line=1, end_line=1),
    )
    finding = Finding(
        id="placement-1",
        goal=AnalysisGoal.PLACEMENT,
        sensor="placement",
        severity=Severity.HIGH,
        title="move foo to B",
        evidence="placement",
        unit_ids=[a.id, b.id],
        action_hint=SuggestedAction.MOVE,
        score=0.9,
    )
    _c, changesets, _d, _a = plan_findings([finding], [a, b], StubProvider())
    assert len(changesets) == 1
    assert changesets[0].action == "move"


def test_sensor_discovery_includes_builtins() -> None:
    from autorefactor.sensors.discovery import discover_sensors

    registry = discover_sensors()
    assert "sim.method" in registry
    assert "structure" in registry
    assert "coverage" in registry
