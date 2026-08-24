"""Coverage path matching + ensure_tests emission + approved-only queue."""

from __future__ import annotations

from pathlib import Path

from autorefactor.changesets.models import Changeset, ChangesetStatus, ChangesetTarget
from autorefactor.changesets.store import ChangesetStore
from autorefactor.coverage_paths import (
    coverage_path_matches,
    normalize_jacoco_rel_path,
)
from autorefactor.domain import CoverageFileStat, CoverageSnapshot
from autorefactor.enrich.coverage_parsers import parse_jacoco_xml
from autorefactor.models import CodeUnit, UnitKind
from autorefactor.plan.followups import _coverage_needs_tests, attach_quality_followups
from autorefactor.queue import save_queue
from autorefactor.testing.gaps import _unit_coverage_percent


def _unit(**kwargs) -> CodeUnit:
    defaults = dict(
        language="java",
        kind=UnitKind.TYPE,
        source_snippet="class X {}",
        content_hash="hash",
    )
    defaults.update(kwargs)
    return CodeUnit(**defaults)


def test_normalize_jacoco_adds_src_main_java() -> None:
    assert (
        normalize_jacoco_rel_path("com/fluttersea/bonus/BonusExceptionHandler.java")
        == "src/main/java/com/fluttersea/bonus/BonusExceptionHandler.java"
    )
    assert (
        normalize_jacoco_rel_path("src/main/java/com/foo/Bar.java")
        == "src/main/java/com/foo/Bar.java"
    )


def test_parse_jacoco_normalizes_to_maven_path(tmp_path: Path) -> None:
    jacoco = tmp_path / "jacoco.xml"
    jacoco.write_text(
        """<?xml version="1.0"?>
<report name="demo">
  <package name="com/fluttersea/bonus">
    <sourcefile name="BonusExceptionHandler.java">
      <counter type="LINE" missed="10" covered="0"/>
    </sourcefile>
  </package>
  <counter type="LINE" missed="10" covered="0"/>
</report>""",
        encoding="utf-8",
    )
    snap = parse_jacoco_xml(jacoco)
    assert snap.files[0].path == (
        "src/main/java/com/fluttersea/bonus/BonusExceptionHandler.java"
    )
    assert snap.files[0].percent == 0.0


def test_coverage_path_matches_repo_prefixed_jacoco_vs_unit() -> None:
    unit = "src/main/java/com/fluttersea/bonus/BonusExceptionHandler.java"
    report = "active/src/main/java/com/fluttersea/bonus/BonusExceptionHandler.java"
    assert coverage_path_matches(unit, report, repo="active")
    assert coverage_path_matches(
        unit,
        "active/com/fluttersea/bonus/BonusExceptionHandler.java",
        repo="active",
    )


def test_unit_coverage_percent_matches_prefixed_jacoco_path() -> None:
    unit = _unit(
        id="u1",
        repo="active",
        path="src/main/java/com/fluttersea/bonus/BonusExceptionHandler.java",
        qualified_name="com.fluttersea.bonus.BonusExceptionHandler",
    )
    coverage = CoverageSnapshot(
        summary="t",
        source="jacoco",
        files=[
            CoverageFileStat(
                path="active/src/main/java/com/fluttersea/bonus/BonusExceptionHandler.java",
                covered_lines=0,
                total_lines=10,
                percent=0.0,
            )
        ],
    )
    assert _unit_coverage_percent(unit, coverage) == 0.0


def test_coverage_needs_tests_despite_existing_test_file() -> None:
    unit = _unit(
        id="u1",
        repo="active",
        path="src/main/java/com/fluttersea/bonus/BonusExceptionHandler.java",
        qualified_name="com.fluttersea.bonus.BonusExceptionHandler",
    )
    coverage = CoverageSnapshot(
        summary="t",
        source="jacoco",
        files=[
            CoverageFileStat(
                path="active/src/main/java/com/fluttersea/bonus/BonusExceptionHandler.java",
                covered_lines=0,
                total_lines=12,
                percent=0.0,
            )
        ],
    )
    existing = [
        "active/src/test/java/com/fluttersea/bonus/BonusExceptionHandlerTest.java",
    ]
    assert _coverage_needs_tests(
        unit, coverage, threshold=80.0, existing_tests=existing
    )
    well = coverage.model_copy(
        update={
            "files": [
                CoverageFileStat(
                    path=coverage.files[0].path,
                    covered_lines=95,
                    total_lines=100,
                    percent=95.0,
                )
            ]
        }
    )
    assert not _coverage_needs_tests(
        unit, well, threshold=80.0, existing_tests=existing
    )


def test_attach_ensure_includes_low_excludes_high_with_test_files() -> None:
    handler = _unit(
        id="h1",
        repo="active",
        path="src/main/java/com/fluttersea/bonus/BonusExceptionHandler.java",
        qualified_name="com.fluttersea.bonus.BonusExceptionHandler",
    )
    service = _unit(
        id="s1",
        repo="active",
        path="src/main/java/com/fluttersea/bonus/BonusService.java",
        qualified_name="com.fluttersea.bonus.BonusService",
    )
    parent = Changeset(
        id="split1",
        title="Split handler",
        action="split",
        brief="b",
        rationale="r",
        confidence=0.9,
        targets=[
            ChangesetTarget(
                repo="active",
                path=handler.path,
                symbol=handler.qualified_name,
                unit_id=handler.id,
            ),
            ChangesetTarget(
                repo="active",
                path=service.path,
                symbol=service.qualified_name,
                unit_id=service.id,
            ),
        ],
        status=ChangesetStatus.PENDING,
    )
    coverage = CoverageSnapshot(
        summary="t",
        source="jacoco",
        overall_percent=40.0,
        files=[
            CoverageFileStat(
                path="active/src/main/java/com/fluttersea/bonus/BonusExceptionHandler.java",
                covered_lines=0,
                total_lines=20,
                percent=0.0,
            ),
            CoverageFileStat(
                path="active/src/main/java/com/fluttersea/bonus/BonusService.java",
                covered_lines=95,
                total_lines=100,
                percent=95.0,
            ),
        ],
    )
    existing = [
        "active/src/test/java/com/fluttersea/bonus/BonusExceptionHandlerTest.java",
        "active/src/test/java/com/fluttersea/bonus/BonusServiceTest.java",
    ]
    out = attach_quality_followups(
        [parent],
        [handler, service],
        coverage=coverage,
        coverage_threshold=80.0,
        existing_tests=existing,
        describe_mode="raw",
    )
    ensure = [c for c in out if c.action == "ensure_tests"]
    assert ensure, "expected ensure_tests for uncovered handler"
    symbols = {t.symbol for c in ensure for t in c.targets}
    assert handler.qualified_name in symbols
    assert service.qualified_name not in symbols
    assert not any(c.action == "document" for c in out)


def test_next_pending_approved_only_while_proceed_pending(tmp_path: Path) -> None:
    out = tmp_path / "out"
    (out / "queue").mkdir(parents=True)
    approved = Changeset(
        id="approved_low",
        title="Approved low conf",
        action="split",
        brief="b",
        rationale="r",
        confidence=0.2,
        user_decision="proceed",
        targets=[ChangesetTarget(repo="r", path="a.java", symbol="A", unit_id="a")],
        status=ChangesetStatus.PENDING,
    )
    high = Changeset(
        id="high_unapproved",
        title="High conf unapproved",
        action="split",
        brief="b",
        rationale="r",
        confidence=0.95,
        targets=[ChangesetTarget(repo="r", path="b.java", symbol="B", unit_id="b")],
        status=ChangesetStatus.PENDING,
    )
    save_queue(out, [high, approved])
    store = ChangesetStore(out / "queue" / "changesets.json")
    first = store.next_pending(min_confidence=0.6)
    assert first.changeset is not None
    assert first.changeset.id == "approved_low"
    assert first.needs_confirmation is False

    store.mark("approved_low", ChangesetStatus.DONE)
    second = store.next_pending(min_confidence=0.6)
    assert second.changeset is not None
    assert second.changeset.id == "high_unapproved"
