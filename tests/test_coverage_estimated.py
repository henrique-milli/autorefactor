from __future__ import annotations

from pathlib import Path

from autorefactor.changesets.models import Changeset, ChangesetStatus, ChangesetTarget
from autorefactor.domain import CodeUnit, CoverageSnapshot, UnitKind, content_hash
from autorefactor.plan.followups import attach_quality_followups
from autorefactor.report.metrics import estimate_metrics
from autorefactor.report.roi import compute_debt_components
from autorefactor.sensors.coverage import _mark_estimated_when_tooling_only, enrich_coverage


def test_empty_snap_with_jacoco_pom_tags_estimated(tmp_path: Path) -> None:
    repo = tmp_path / "janus-bonus"
    repo.mkdir()
    (repo / "pom.xml").write_text(
        "<project><build><plugins><plugin>"
        "<artifactId>jacoco-maven-plugin</artifactId>"
        "</plugin></plugins></build></project>",
        encoding="utf-8",
    )
    snap = CoverageSnapshot(
        summary="missing",
        source="missing",
        files=[],
        repos_without_coverage=["janus-bonus"],
    )
    marked = _mark_estimated_when_tooling_only(snap, [repo])
    assert marked.source == "estimated"
    assert marked.files == []
    assert "janus-bonus" in marked.repos_without_coverage

    enriched = enrich_coverage(workspace=tmp_path, repos=[repo])
    assert enriched.source == "estimated"


def test_estimated_coverage_skips_ensure_tests_and_caps_debt(tmp_path: Path) -> None:
    repo_root = tmp_path / "janus-bonus"
    repo_root.mkdir()
    (repo_root / "pom.xml").write_text(
        "<project><build><plugins><plugin>"
        "<artifactId>jacoco-maven-plugin</artifactId>"
        "</plugin></plugins></build></project>",
        encoding="utf-8",
    )
    src = "janus-bonus/src/main/java/com/example/v1/service/BonusAggregationService.java"
    u = CodeUnit(
        id="u-bonus",
        repo="janus-bonus",
        path=src,
        language="java",
        kind=UnitKind.TYPE,
        qualified_name="com.example.v1.service.BonusAggregationService",
        source_snippet="class BonusAggregationService {}",
        content_hash=content_hash("u-bonus"),
    )
    parent = Changeset(
        id="split1",
        title="Split BonusAggregationService",
        action="split",
        brief="b",
        rationale="r",
        confidence=0.99,
        targets=[
            ChangesetTarget(
                repo="janus-bonus",
                path=src,
                unit_id="u-bonus",
                symbol="com.example.v1.service.BonusAggregationService",
            )
        ],
        status=ChangesetStatus.PENDING,
    )
    coverage = CoverageSnapshot(
        summary="estimated",
        source="estimated",
        files=[],
        repos_without_coverage=["janus-bonus"],
    )
    out = attach_quality_followups(
        [parent],
        [u],
        coverage=coverage,
        workspace=tmp_path,
        repos=[repo_root],
    )
    actions = {c.action for c in out}
    assert "ensure_tests" in actions
    assert "split" in actions

    debt = compute_debt_components(
        split_cs=1,
        clone_paths=0,
        test_cs=1,
        doc_cs=0,
        slop_score=0.0,
        queue_n=2,
        findings_structure=0,
        findings_clone=0,
        coverage_estimated=True,
        test_inventory_ratio=0.5,
    )
    assert debt["coverage"] < 15.0

    metrics = estimate_metrics(
        units=[u],
        changesets=[parent],
        coverage=coverage,
        repos=[repo_root],
    )
    warnings = metrics.get("quality_warnings") or []
    assert any("Coverage estimated only" in w for w in warnings)
