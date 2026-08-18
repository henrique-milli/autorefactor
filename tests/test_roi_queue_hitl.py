"""Tests for finding collapse, quality follow-ups, and cross-repo confidence."""

from __future__ import annotations

from pathlib import Path

from autorefactor.changesets.models import Changeset, ChangesetStatus, ChangesetTarget
from autorefactor.domain import (
    AnalysisGoal,
    CodeUnit,
    CoverageFileStat,
    CoverageSnapshot,
    Finding,
    Severity,
    SuggestedAction,
    UnitKind,
    content_hash,
)
from autorefactor.plan.followups import FOLLOWUP_CONFIDENCE, attach_quality_followups
from autorefactor.plan.planner import calibrate_confidence
from autorefactor.sensors.collapse import collapse_findings_by_unit_set
from autorefactor.sensors.complexity import measure_complexity_detail


def _unit(uid: str, *, repo: str = "r", path: str = "a.py", name: str = "f") -> CodeUnit:
    return CodeUnit(
        id=uid,
        repo=repo,
        path=path,
        language="python",
        kind=UnitKind.FUNCTION,
        qualified_name=f"mod.{name}",
        source_snippet=f"def {name}():\n    return 1\n",
        content_hash=content_hash(uid),
    )


def _finding(
    *,
    fid: str,
    sensor: str,
    action: SuggestedAction,
    unit_ids: list[str],
    score: float = 0.8,
    goal: AnalysisGoal = AnalysisGoal.STRUCTURE,
    metrics: dict | None = None,
) -> Finding:
    return Finding(
        id=fid,
        goal=goal,
        sensor=sensor,
        severity=Severity.MEDIUM,
        title=f"{sensor}:{fid}",
        evidence="e",
        unit_ids=unit_ids,
        action_hint=action,
        score=score,
        metrics=metrics or {},
    )


def test_collapse_prefers_clone_over_split() -> None:
    sim = _finding(
        fid="sim1",
        sensor="sim.method",
        action=SuggestedAction.EXTRACT_SHARED,
        unit_ids=["u1", "u2"],
        score=0.7,
        goal=AnalysisGoal.METHOD_SIMILARITY,
    )
    struct = _finding(
        fid="st1",
        sensor="structure",
        action=SuggestedAction.SPLIT,
        unit_ids=["u1", "u2"],
        score=0.9,
    )
    cx = _finding(
        fid="cx1",
        sensor="complexity",
        action=SuggestedAction.SPLIT,
        unit_ids=["u1"],
        score=0.95,
    )
    out = collapse_findings_by_unit_set([sim, struct, cx])
    ids = {f.id for f in out}
    assert "sim1" in ids
    assert "st1" not in ids  # same unit-set collapsed / demoted
    assert "cx1" not in ids  # demoted — overlaps clone units


def test_collapse_keeps_unrelated_split() -> None:
    sim = _finding(
        fid="sim1",
        sensor="sim.method",
        action=SuggestedAction.EXTRACT_SHARED,
        unit_ids=["u1", "u2"],
        score=0.8,
        goal=AnalysisGoal.METHOD_SIMILARITY,
    )
    other = _finding(
        fid="st2",
        sensor="structure",
        action=SuggestedAction.SPLIT,
        unit_ids=["u9"],
        score=0.7,
    )
    out = collapse_findings_by_unit_set([sim, other])
    assert {f.id for f in out} == {"sim1", "st2"}


def test_attach_followups_document_and_ensure_tests_deps(tmp_path: Path) -> None:
    u = _unit("u1", path="mod.py", name="add")
    # Undocumented function → doc gap
    parent = Changeset(
        id="parent1",
        title="Split add",
        action="split",
        brief="b",
        rationale="r",
        confidence=0.85,
        targets=[ChangesetTarget(repo="r", path="mod.py", unit_id="u1", symbol="mod.add")],
        status=ChangesetStatus.PENDING,
        metrics_delta={"estimated_loc_removed": 0, "loc_under_review": 10},
    )
    coverage = CoverageSnapshot(
        summary="low",
        source="coverage.py",
        files=[CoverageFileStat(path="r/mod.py", covered_lines=1, total_lines=20, percent=5.0)],
    )
    out = attach_quality_followups(
        [parent],
        [u],
        coverage=coverage,
        coverage_threshold=80.0,
    )
    actions = {c.action for c in out}
    assert "split" in actions
    assert "ensure_tests" in actions
    # Coverage-first: ensure_tests → split; document follow-ups suppressed
    assert [c.action for c in out] == ["ensure_tests", "split"]
    split = next(c for c in out if c.action == "split")
    ensure = next(c for c in out if c.action == "ensure_tests")
    assert ensure.confidence == FOLLOWUP_CONFIDENCE
    assert ensure.id in split.depends_on
    assert split.metrics_delta.get("needs_coverage") is True
    assert split.metrics_delta.get("needs_write_docs") is True
    assert not any(c.action == "document" for c in out)
    _ = tmp_path


def test_attach_followups_skips_ensure_tests_when_test_file_exists(
    tmp_path: Path,
) -> None:
    """janus-bonus-style Java unit with *Test.java should not emit ensure_tests."""
    repo_root = tmp_path / "janus-bonus"
    repo_root.mkdir()
    src = (
        "janus-bonus/src/main/java/com/example/v1/service/BonusAggregationService.java"
    )
    test = (
        "janus-bonus/src/test/java/com/example/v1/service/BonusAggregationServiceTest.java"
    )
    (repo_root / src).parent.mkdir(parents=True, exist_ok=True)
    (repo_root / test).parent.mkdir(parents=True, exist_ok=True)
    (repo_root / src).write_text("class BonusAggregationService {}\n", encoding="utf-8")
    (repo_root / test).write_text("class BonusAggregationServiceTest {}\n", encoding="utf-8")

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
        metrics_delta={"estimated_loc_removed": 50, "loc_under_review": 200},
    )
    coverage = CoverageSnapshot(
        summary="measured",
        source="jacoco",
        files=[
            CoverageFileStat(
                path=src,
                percent=92.0,
                covered_lines=90,
                total_lines=100,
            )
        ],
    )
    eval_ws = tmp_path
    out = attach_quality_followups(
        [parent],
        [u],
        coverage=coverage,
        coverage_threshold=80.0,
        workspace=eval_ws,
        repos=[repo_root],
    )
    actions = {c.action for c in out}
    assert "split" in actions
    assert "ensure_tests" not in actions
    split = next(c for c in out if c.action == "split")
    assert not any(
        dep for dep in (split.depends_on or []) if "ensure" in dep.lower()
    )
    assert split.metrics_delta.get("needs_coverage") is not True


def test_empty_jacoco_index_emits_ensure_tests_despite_mock_suite(
    tmp_path: Path,
) -> None:
    """Empty Jacoco index + structural split must prep even when a mock *Test.java exists."""
    repo_root = tmp_path / "janus-account"
    repo_root.mkdir()
    src = (
        "janus-account/src/main/java/com/example/v1/service/PersistenceService.java"
    )
    test = (
        "janus-account/src/test/java/com/example/v1/service/PersistenceServiceTest.java"
    )
    (repo_root / src).parent.mkdir(parents=True, exist_ok=True)
    (repo_root / test).parent.mkdir(parents=True, exist_ok=True)
    (repo_root / src).write_text("class PersistenceService {}\n", encoding="utf-8")
    (repo_root / test).write_text(
        "@WebMvcTest class PersistenceServiceTest {}\n", encoding="utf-8"
    )

    u = CodeUnit(
        id="u-persist",
        repo="janus-account",
        path=src,
        language="java",
        kind=UnitKind.TYPE,
        qualified_name="com.example.v1.service.PersistenceService",
        source_snippet="class PersistenceService {}",
        content_hash=content_hash("u-persist"),
    )
    parent = Changeset(
        id="split-persist",
        title="Split PersistenceService",
        action="split",
        brief="b",
        rationale="r",
        confidence=0.99,
        targets=[
            ChangesetTarget(
                repo="janus-account",
                path=src,
                unit_id="u-persist",
                symbol="com.example.v1.service.PersistenceService",
            )
        ],
        status=ChangesetStatus.PENDING,
        metrics_delta={"estimated_loc_removed": 40, "loc_under_review": 180},
    )
    coverage = CoverageSnapshot(
        summary="estimated",
        source="jacoco",
        files=[],
        repos_without_coverage=["janus-account"],
    )
    out = attach_quality_followups(
        [parent],
        [u],
        coverage=coverage,
        coverage_threshold=80.0,
        workspace=tmp_path,
        repos=[repo_root],
    )
    actions = {c.action for c in out}
    assert "ensure_tests" in actions
    ensure = next(c for c in out if c.action == "ensure_tests")
    assert ensure.targets
    split = next(c for c in out if c.action == "split")
    assert split.metrics_delta.get("needs_coverage") is True


def test_calibrate_confidence_cross_repo_boost() -> None:
    a = _finding(
        fid="a",
        sensor="sim.method",
        action=SuggestedAction.EXTRACT_SHARED,
        unit_ids=["u1", "u2"],
        score=0.7,
        goal=AnalysisGoal.METHOD_SIMILARITY,
        metrics={"cross_repo": 1.0},
    )
    by = {a.id: a}
    base = calibrate_confidence(finding_ids=["a"], findings_by_id=by, llm_confidence=0.7)
    # Same without cross_repo
    b = a.model_copy(update={"metrics": {}, "id": "b"})
    by2 = {b.id: b}
    plain = calibrate_confidence(finding_ids=["b"], findings_by_id=by2, llm_confidence=0.7)
    assert base > plain


def test_nested_loop_depth_detected() -> None:
    src = (
        "def walk(items):\n"
        "    for a in items:\n"
        "        for b in a:\n"
        "            print(b)\n"
    )
    nesting, cx, catch, loops = measure_complexity_detail(src, "python")
    assert loops >= 2
    assert nesting >= 1
    assert cx >= 1
    assert catch == 0


def test_collapse_duplicate_slop_doc_followups() -> None:
    """Repo-root document follow-up dropped when file-scoped sibling exists."""
    from autorefactor.plan.followups import _collapse_duplicate_followups

    file_doc = Changeset(
        id="doc-file",
        title="Document gaps for: Draft mapping in PokerstarsProductMappingService",
        action="document",
        brief="b",
        rationale="1 target missing docs",
        confidence=0.55,
        targets=[
            ChangesetTarget(
                repo="janus-bonus",
                path="src/main/java/com/example/PokerstarsProductMappingService.java",
                unit_id="u-type",
                symbol="com.example.PokerstarsProductMappingService",
            )
        ],
        metrics_delta={"followup": True, "parent_changeset_id": "parent-file"},
        evidence_refs=["slop:draft_map:u-type"],
    )
    root_doc = Changeset(
        id="doc-root",
        title="Document gaps for: Draft mapping in janus-bonus",
        action="document",
        brief="b",
        rationale="1 target missing docs",
        confidence=0.55,
        targets=[ChangesetTarget(repo="janus-bonus", path=".", unit_id="u-repo", symbol="janus-bonus")],
        metrics_delta={"followup": True, "parent_changeset_id": "parent-root"},
        evidence_refs=["slop:draft_map:u-type"],
    )
    split = Changeset(
        id="split1",
        title="Split PokerstarsProductMappingService",
        action="split",
        brief="b",
        rationale="r",
        confidence=0.99,
        targets=[
            ChangesetTarget(
                repo="janus-bonus",
                path="src/main/java/com/example/PokerstarsProductMappingService.java",
                unit_id="u-type",
                symbol="com.example.PokerstarsProductMappingService",
            )
        ],
    )
    out = _collapse_duplicate_followups([split, file_doc, root_doc])
    doc_ids = {c.id for c in out if c.action == "document"}
    assert doc_ids == {"doc-file"}


def test_coverage_first_split_before_document_followups() -> None:
    u = _unit("u1", path="mod.py", name="add")
    parent = Changeset(
        id="parent1",
        title="Split add",
        action="split",
        brief="b",
        rationale="r",
        confidence=0.99,
        targets=[ChangesetTarget(repo="r", path="mod.py", unit_id="u1", symbol="mod.add")],
        status=ChangesetStatus.PENDING,
    )
    coverage = CoverageSnapshot(
        summary="low",
        source="coverage.py",
        files=[CoverageFileStat(path="r/mod.py", covered_lines=1, total_lines=20, percent=5.0)],
    )
    out = attach_quality_followups([parent], [u], coverage=coverage, coverage_threshold=80.0)
    actions = [c.action for c in out]
    assert actions == ["ensure_tests", "split"]
    assert "document" not in actions


def test_structural_primary_before_document_followups_without_ensure_tests() -> None:
    """Document follow-ups defer until structural primaries when ensure_tests is absent."""
    from autorefactor.changesets.ranking import prep_rank

    split = Changeset(
        id="split1",
        title="Split add",
        action="split",
        brief="b",
        rationale="r",
        confidence=0.99,
        targets=[ChangesetTarget(repo="r", path="mod.py", unit_id="u1", symbol="mod.add")],
    )
    doc_followup = Changeset(
        id="doc1",
        title="Document gaps for: Split add",
        action="document",
        brief="b",
        rationale="r",
        confidence=0.55,
        targets=[ChangesetTarget(repo="r", path="mod.py", unit_id="u1", symbol="mod.add")],
        metrics_delta={"followup": True, "parent_changeset_id": "split1"},
    )
    assert prep_rank(split, coverage_first=False) < prep_rank(doc_followup, coverage_first=False)


def test_estimated_empty_index_skips_gap_followups_without_tooling(
    tmp_path: Path,
) -> None:
    """Missing coverage (no Jacoco tooling) must not queue ensure_tests without split debt."""
    repo_root = tmp_path / "janus-bonus"
    repo_root.mkdir()
    padding = CodeUnit(
        id="u-pad",
        repo="janus-bonus",
        path="src/test/java/com/example/BonusModelCoverageTest.java",
        language="java",
        kind=UnitKind.TYPE,
        qualified_name="com.example.BonusModelCoverageTest",
        source_snippet="class BonusModelCoverageTest {}",
        content_hash=content_hash("u-pad"),
    )
    prod = CodeUnit(
        id="u-prod",
        repo="janus-bonus",
        path="src/main/java/com/example/v1/service/BonusAggregationService.java",
        language="java",
        kind=UnitKind.TYPE,
        qualified_name="com.example.v1.service.BonusAggregationService",
        source_snippet="class BonusAggregationService {}",
        content_hash=content_hash("u-prod"),
    )
    merge = Changeset(
        id="merge1",
        title="Merge duplicate helpers",
        action="merge",
        brief="b",
        rationale="r",
        confidence=0.99,
        targets=[
            ChangesetTarget(
                repo="janus-bonus",
                path=prod.path,
                unit_id=prod.id,
                symbol=prod.qualified_name,
            )
        ],
        status=ChangesetStatus.PENDING,
    )
    coverage = CoverageSnapshot(
        summary="missing",
        source="missing",
        files=[],
        repos_without_coverage=["janus-bonus"],
    )
    out = attach_quality_followups(
        [merge],
        [padding, prod],
        coverage=coverage,
        coverage_threshold=80.0,
        coverage_finding_count=0,
    )
    assert "ensure_tests" not in {c.action for c in out}


def test_estimated_empty_index_arms_ensure_tests_without_split_debt(tmp_path: Path) -> None:
    """Jacoco-configured estimated snapshot must queue ensure_tests on production TYPE."""
    repo_root = tmp_path / "janus-bonus"
    repo_root.mkdir()
    (repo_root / "pom.xml").write_text(
        "<project><build><plugins><plugin>"
        "<artifactId>jacoco-maven-plugin</artifactId>"
        "</plugin></plugins></build></project>",
        encoding="utf-8",
    )
    prod = CodeUnit(
        id="u-prod",
        repo="janus-bonus",
        path="src/main/java/com/example/v1/service/BonusAggregationService.java",
        language="java",
        kind=UnitKind.TYPE,
        qualified_name="com.example.v1.service.BonusAggregationService",
        source_snippet="class BonusAggregationService {}",
        content_hash=content_hash("u-prod"),
    )
    merge = Changeset(
        id="merge1",
        title="Merge duplicate helpers",
        action="merge",
        brief="b",
        rationale="r",
        confidence=0.99,
        targets=[
            ChangesetTarget(
                repo="janus-bonus",
                path=prod.path,
                unit_id=prod.id,
                symbol=prod.qualified_name,
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
        [merge],
        [prod],
        coverage=coverage,
        workspace=tmp_path,
        repos=[repo_root],
        coverage_threshold=80.0,
        coverage_finding_count=1,
    )
    ensure = [c for c in out if c.action == "ensure_tests"]
    assert ensure
    paths = {(t.path or "").replace("\\", "/") for c in ensure for t in c.targets}
    assert any("src/main/" in p for p in paths)
    assert not any("CoverageTest" in p for p in paths)


def test_estimated_empty_index_arms_ensure_tests_for_split_debt(tmp_path: Path) -> None:
    """Split-heavy queue with empty Jacoco index should arm ensure_tests prep."""
    from autorefactor.changesets.ranking import coverage_first_mode

    repo_root = tmp_path / "janus-bonus"
    repo_root.mkdir()
    prod = CodeUnit(
        id="u-prod",
        repo="janus-bonus",
        path="src/main/java/com/example/v1/service/BonusAggregationService.java",
        language="java",
        kind=UnitKind.TYPE,
        qualified_name="com.example.v1.service.BonusAggregationService",
        source_snippet="class BonusAggregationService {}",
        content_hash=content_hash("u-prod"),
    )
    split = Changeset(
        id="split1",
        title="Split BonusAggregationService",
        action="split",
        brief="b",
        rationale="r",
        confidence=0.99,
        targets=[
            ChangesetTarget(
                repo="janus-bonus",
                path=prod.path,
                unit_id=prod.id,
                symbol=prod.qualified_name,
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
        [split],
        [prod],
        coverage=coverage,
        workspace=tmp_path,
        repos=[repo_root],
        coverage_threshold=80.0,
    )
    assert "ensure_tests" in {c.action for c in out}
    assert coverage_first_mode(out) is True


def test_collapse_primary_ensure_tests_over_followup_sibling() -> None:
    """At most one ensure_tests per unit_id; primary wins over follow-up."""
    from autorefactor.plan.followups import _collapse_duplicate_followups

    uid = "u-pam"
    primary = Changeset(
        id="ensure-primary",
        title="Ensure tests for PamClientException",
        action="ensure_tests",
        brief="Ask the user before applying (HITL).",
        rationale="mock-only prep",
        confidence=0.90,
        targets=[
            ChangesetTarget(
                repo="janus-bonus",
                path="src/main/java/com/example/PamClientException.java",
                unit_id=uid,
                symbol="com.example.PamClientException",
            )
        ],
        metrics_delta={"kind": "test_quality"},
    )
    followup = Changeset(
        id="ensure-follow",
        title="Ensure tests before/with: Split PamClientException",
        action="ensure_tests",
        brief="Ask the user before applying (HITL).",
        rationale="below 80% threshold",
        confidence=0.55,
        targets=[
            ChangesetTarget(
                repo="janus-bonus",
                path="src/main/java/com/example/PamClientException.java",
                unit_id=uid,
                symbol="com.example.PamClientException",
            )
        ],
        metrics_delta={"followup": True, "parent_changeset_id": "split1"},
    )
    split = Changeset(
        id="split1",
        title="Split PamClientException",
        action="split",
        brief="b",
        rationale="r",
        confidence=0.99,
        targets=[ChangesetTarget(repo="janus-bonus", path="src/main/java/com/example/PamClientException.java", unit_id=uid)],
    )
    out = _collapse_duplicate_followups([split, primary, followup])
    ensure = [c for c in out if c.action == "ensure_tests"]
    assert len(ensure) == 1
    assert ensure[0].id == "ensure-primary"


def test_primary_ensure_tests_arms_coverage_first_ranking() -> None:
    """Primary (non-followup) ensure_tests must arm coverage_first_mode."""
    from autorefactor.changesets.ranking import coverage_first_mode, prep_rank

    ensure = Changeset(
        id="ensure1",
        title="Ensure tests for add",
        action="ensure_tests",
        brief="b",
        rationale="r",
        confidence=0.7,
        targets=[ChangesetTarget(repo="r", path="mod.py", unit_id="u1", symbol="mod.add")],
    )
    split = Changeset(
        id="split1",
        title="Split add",
        action="split",
        brief="b",
        rationale="r",
        confidence=0.99,
        targets=[ChangesetTarget(repo="r", path="mod.py", unit_id="u1", symbol="mod.add")],
    )
    doc = Changeset(
        id="doc1",
        title="Document add",
        action="document",
        brief="b",
        rationale="r",
        confidence=0.55,
        targets=[ChangesetTarget(repo="r", path="mod.py", unit_id="u1", symbol="mod.add")],
    )
    items = [doc, split, ensure]
    assert coverage_first_mode(items) is True
    ranked = sorted(items, key=lambda c: (prep_rank(c, coverage_first=True), -c.confidence, c.id))
    assert ranked[0].action == "ensure_tests"
    assert ranked[1].action == "split"
    assert ranked[2].action == "document"


def test_draft_map_slop_emits_ensure_tests_for_caller_service() -> None:
    """Draft_map parent (hashed evidence refs + metadata kind) yields caller ensure_tests."""
    mapper_path = "janus-bonus/src/main/java/com/example/PokerstarsProductMappingService.java"
    caller_path = "janus-bonus/src/main/java/com/example/v1/service/BonusAggregationService.java"
    mapper = CodeUnit(
        id="u-mapper",
        repo="janus-bonus",
        path=mapper_path,
        language="java",
        kind=UnitKind.TYPE,
        qualified_name="com.example.PokerstarsProductMappingService",
        source_snippet=(
            "class PokerstarsProductMappingService {\n"
            "  Map<String,String> m = new HashMap<>();\n"
            "  String mapToProductId(String x) { return m.get(x); }\n"
            "}\n"
        ),
        content_hash=content_hash("u-mapper"),
    )
    caller = CodeUnit(
        id="u-caller",
        repo="janus-bonus",
        path=caller_path,
        language="java",
        kind=UnitKind.FUNCTION,
        qualified_name="com.example.v1.service.BonusAggregationService.processBonusBalance",
        parent_id="u-svc",
        source_snippet=(
            "void processBonusBalance() {\n"
            "  mapper.mapToProductId(\"ASSET_TYPE_CASH\");\n"
            "}\n"
        ),
        content_hash=content_hash("u-caller"),
    )
    # Janus planner hashes evidence refs — detection must use metrics.kind, not startswith.
    hashed_ref = content_hash("slop:draft_map:u-mapper")
    parent = Changeset(
        id="split-mapper",
        title="Split PokerstarsProductMappingService",
        action="split",
        brief="Draft mapping silent skip risk",
        rationale="Draft in-memory mapping with constructor-initialized HashMap defaults",
        confidence=0.99,
        targets=[
            ChangesetTarget(
                repo="janus-bonus",
                path=mapper_path,
                unit_id="u-mapper",
                symbol="com.example.PokerstarsProductMappingService",
            )
        ],
        evidence_refs=[hashed_ref],
        metrics_delta={"kind": "draft_in_memory_map"},
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
        [mapper, caller],
        coverage=coverage,
        coverage_threshold=80.0,
    )
    ensure = next((c for c in out if c.action == "ensure_tests"), None)
    assert ensure is not None
    assert "Do not expand Jacoco padding" in ensure.brief
    paths = {(t.path or "").replace("\\", "/") for t in ensure.targets}
    assert any("src/main/" in p and "Test" not in p for p in paths)
    assert not any(p in {"", ".", "janus-bonus"} for p in paths)
    symbols = {t.symbol for t in ensure.targets}
    assert any("BonusAggregationService" in (s or "") for s in symbols)


def test_primary_padding_ensure_tests_remapped_when_coverage_estimated() -> None:
    """Primary ensure_tests on CoverageTest remaps to production under estimated coverage."""
    padding = CodeUnit(
        id="u-pad",
        repo="janus-bonus",
        path="src/test/java/com/example/BonusModelCoverageTest.java",
        language="java",
        kind=UnitKind.TYPE,
        qualified_name="com.example.BonusModelCoverageTest",
        source_snippet="class BonusModelCoverageTest {}",
        content_hash=content_hash("u-pad"),
    )
    prod = CodeUnit(
        id="u-prod",
        repo="janus-bonus",
        path="src/main/java/com/example/BonusModel.java",
        language="java",
        kind=UnitKind.TYPE,
        qualified_name="com.example.BonusModel",
        source_snippet="class BonusModel {}",
        content_hash=content_hash("u-prod"),
    )
    cs = Changeset(
        id="ensure-pad",
        title="Ensure tests for BonusModelCoverageTest",
        action="ensure_tests",
        brief="b",
        rationale="coverage padding",
        confidence=0.7,
        targets=[
            ChangesetTarget(
                repo="janus-bonus",
                path=padding.path,
                unit_id=padding.id,
                symbol=padding.qualified_name,
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
        [cs],
        [padding, prod],
        coverage=coverage,
        coverage_threshold=80.0,
    )
    ensure = next((c for c in out if c.action == "ensure_tests"), None)
    assert ensure is not None
    paths = {(t.path or "").replace("\\", "/") for t in ensure.targets}
    assert prod.path in paths
    assert padding.path not in paths
    assert str((ensure.metrics_delta or {}).get("kind") or "") in {
        "",
        "remapped_from_padding",
    }


def test_strip_primary_repo_root_document_duplicate() -> None:
    """Primary (non-followup) repo-root document dropped when file-scoped sibling shares kind."""
    from autorefactor.plan.followups import _strip_repo_root_doc_duplicates

    file_doc = Changeset(
        id="doc-file",
        title="Document gaps for: Draft mapping in PokerstarsProductMappingService",
        action="document",
        brief="b",
        rationale="1 target missing docs",
        confidence=0.55,
        targets=[
            ChangesetTarget(
                repo="janus-bonus",
                path="src/main/java/com/example/PokerstarsProductMappingService.java",
                unit_id="u-type",
                symbol="com.example.PokerstarsProductMappingService",
            )
        ],
        evidence_refs=[content_hash("slop:draft_map:u-type")],
        metrics_delta={"kind": "draft_in_memory_map"},
    )
    root_doc = Changeset(
        id="doc-root",
        title="Document gaps for: Draft mapping in janus-bonus",
        action="document",
        brief="b",
        rationale="1 target missing docs",
        confidence=0.55,
        targets=[ChangesetTarget(repo="janus-bonus", path=".", unit_id="u-repo", symbol="janus-bonus")],
        evidence_refs=[content_hash("slop:draft_map:file:x")],
        metrics_delta={"kind": "draft_in_memory_map"},
    )
    out = _strip_repo_root_doc_duplicates([file_doc, root_doc])
    assert {c.id for c in out} == {"doc-file"}


def test_framework_sensitive_split_requires_mapstruct_annotations() -> None:
    from autorefactor.changesets.store import (
        apply_confidence_gates,
        changeset_needs_hitl,
        framework_sensitive_split,
    )

    component_mapper = Changeset(
        id="split-mapper",
        title="Split BonusResponseMapper.mergeAggregations",
        action="split",
        brief="Hand-written @Component mapper",
        rationale="Extract mergeAggregations",
        confidence=0.54,
        targets=[
            ChangesetTarget(
                repo="janus-bonus",
                path="src/main/java/com/example/BonusResponseMapper.java",
                symbol="com.example.BonusResponseMapper",
                unit_id="u-mapper",
            )
        ],
    )
    assert framework_sensitive_split(component_mapper) is False
    hitl, reason = changeset_needs_hitl(component_mapper, 0.6)
    assert reason != "framework_sensitive_split"

    mapstruct = component_mapper.model_copy(
        update={
            "brief": "MapStruct @Mapper interface with @Mapping(source=...) generation",
            "confidence": 0.85,
        }
    )
    assert framework_sensitive_split(mapstruct) is True
    capped = apply_confidence_gates(mapstruct, min_confidence=0.6)
    assert capped.confidence < mapstruct.confidence


def test_wiring_orphan_kafka_emits_integration_ensure_tests() -> None:
    handler_type = CodeUnit(
        id="u-svc",
        repo="janus-bonus",
        path="src/main/java/com/example/BonusAggregationService.java",
        language="java",
        kind=UnitKind.TYPE,
        qualified_name="com.example.BonusAggregationService",
        source_snippet="class BonusAggregationService {}",
        content_hash=content_hash("u-svc"),
    )
    handler_fn = CodeUnit(
        id="u-fn",
        repo="janus-bonus",
        path="src/main/java/com/example/BonusAggregationService.java",
        language="java",
        kind=UnitKind.FUNCTION,
        qualified_name="com.example.BonusAggregationService.processPokerstarsTransaction",
        parent_id="u-svc",
        source_snippet="void processPokerstarsTransaction() {}",
        content_hash=content_hash("u-fn"),
    )
    app_main = CodeUnit(
        id="u-main",
        repo="janus-bonus",
        path="src/main/java/com/example/JanusBonusApplication.java",
        language="java",
        kind=UnitKind.FUNCTION,
        qualified_name="com.example.JanusBonusApplication.main",
        source_snippet="void main() {}",
        content_hash=content_hash("u-main"),
    )
    parent = Changeset(
        id="doc-kafka",
        title="Orphan Kafka/messaging wiring in janus-bonus",
        action="document",
        brief="wire_kafka_consumer or remove_dead_wiring",
        rationale="Kafka wiring without @KafkaListener consumer",
        confidence=0.9,
        targets=[
            ChangesetTarget(
                repo="janus-bonus",
                path=handler_type.path,
                unit_id=handler_type.id,
                symbol=handler_type.qualified_name,
            )
        ],
        metrics_delta={"kind": "orphan_kafka"},
        status=ChangesetStatus.PENDING,
    )
    out = attach_quality_followups(
        [parent],
        [handler_type, handler_fn, app_main],
        coverage=CoverageSnapshot(summary="estimated", source="estimated", files=[]),
        coverage_threshold=80.0,
    )
    ensure = next((c for c in out if c.action == "ensure_tests"), None)
    assert ensure is not None
    assert "@EmbeddedKafka" in ensure.brief or "Testcontainers" in ensure.brief
    symbols = {t.symbol for t in ensure.targets}
    assert any("processPokerstarsTransaction" in (s or "") for s in symbols)
    assert not any("JanusBonusApplication.main" in (s or "") for s in symbols)


def test_slop_kinds_and_public_service_move_require_hitl() -> None:
    from autorefactor.changesets.store import changeset_needs_hitl

    secret = Changeset(
        id="ff32204199ac5273",
        title="Document committed credential in application.yaml",
        action="document",
        brief="mongodb+srv:// user:pass@cluster",
        rationale="committed_secret",
        confidence=0.92,
        targets=[
            ChangesetTarget(
                repo="janus-account",
                path="src/main/resources/application.yaml",
                symbol="application.yaml",
            )
        ],
        metrics_delta={"kind": "committed_secret"},
    )
    disabled = Changeset(
        id="fe97625b3320349a",
        title="Split ConfigurationValidator",
        action="split",
        brief="re-enable startup validation",
        rationale="disabled @PostConstruct TODO",
        confidence=0.79,
        targets=[
            ChangesetTarget(
                repo="janus-bonus",
                path="src/main/java/com/example/ConfigurationValidator.java",
                symbol="com.example.ConfigurationValidator",
            )
        ],
        metrics_delta={"kind": "disabled_startup_validation"},
    )
    draft = Changeset(
        id="4096a98dfe44ed21",
        title="Extract shared draft map",
        action="extract_shared",
        brief="draft in-memory mapping",
        rationale="replace HashMap defaults with Mongo config",
        confidence=0.76,
        targets=[
            ChangesetTarget(
                repo="janus-bonus",
                path="src/main/java/com/example/PokerstarsProductMappingService.java",
                symbol="com.example.PokerstarsProductMappingService",
            )
        ],
        metrics_delta={"kind": "draft_in_memory_map"},
    )
    move = Changeset(
        id="b56aa95fc418cf8b",
        title="Move aggregateBonuses to AggregatedBonusData",
        action="move",
        brief="cosine=0.967 placement into BonusAggregationService",
        rationale="relocate public service method",
        confidence=0.97,
        targets=[
            ChangesetTarget(
                repo="janus-bonus",
                path="src/main/java/com/example/BonusAggregationService.java",
                symbol="com.example.BonusAggregationService.aggregateBonuses",
            )
        ],
    )
    dto_move = Changeset(
        id="dto-persist",
        title="Move helper into BonusData",
        action="move",
        brief="injects BonusRepository into AggregatedBonusData DTO",
        rationale="placement into data type with persistence",
        confidence=0.91,
        targets=[
            ChangesetTarget(
                repo="janus-bonus",
                path="src/main/java/com/example/dto/AggregatedBonusData.java",
                symbol="com.example.dto.AggregatedBonusData",
            )
        ],
    )

    for cs, reason in (
        (secret, "security_sensitive"),
        (disabled, "slop_sensitive"),
        (draft, "slop_sensitive"),
        (dto_move, "placement_risk"),
    ):
        needs, hitl_reason = changeset_needs_hitl(cs, 0.6)
        assert needs is True
        assert hitl_reason == reason

    needs, reason = changeset_needs_hitl(move, 0.6)
    assert needs is False
    assert reason is None


def test_batched_document_followups_collapse_many_siblings() -> None:
    from autorefactor.plan.followups import _consolidate_document_followups

    split = Changeset(
        id="split1",
        title="Split PersistenceService",
        action="split",
        brief="b",
        rationale="r",
        confidence=0.99,
        targets=[
            ChangesetTarget(
                repo="janus-account",
                path="src/main/java/PersistenceService.java",
                unit_id="u1",
                symbol="PersistenceService",
            )
        ],
    )
    docs = [
        Changeset(
            id=f"doc{i}",
            title=f"Document gaps for: item {i}",
            action="document",
            brief="b",
            rationale="missing docs",
            confidence=0.55,
            targets=[
                ChangesetTarget(
                    repo="janus-account",
                    path=f"src/main/java/File{i}.java",
                    unit_id=f"u{i}",
                    symbol=f"File{i}",
                )
            ],
            metrics_delta={"followup": True, "parent_changeset_id": "split1"},
        )
        for i in range(5)
    ]
    out = _consolidate_document_followups([split, *docs])
    doc_items = [c for c in out if c.action == "document"]
    assert len(doc_items) == 1
    assert doc_items[0].metrics_delta.get("batched_document_prep") is True


def test_self_merge_and_uncovered_structural_require_hitl() -> None:
    from autorefactor.changesets.store import changeset_needs_hitl

    self_merge = Changeset(
        id="a1d90ed41bcba051",
        title="Merge duplicate fill overloads",
        action="merge",
        brief="identical qualified symbol on both targets",
        rationale="dedup",
        confidence=0.99,
        targets=[
            ChangesetTarget(
                repo="janus-account",
                path="src/main/java/A.java",
                symbol="com.example.A.fill",
            ),
            ChangesetTarget(
                repo="janus-account",
                path="src/main/java/A.java",
                symbol="com.example.A.fill",
            ),
        ],
    )
    needs, reason = changeset_needs_hitl(self_merge, 0.6)
    assert needs is True
    assert reason == "self_merge"

    split = Changeset(
        id="159071f08b598f0c",
        title="Split processPokerstarsTransaction",
        action="split",
        brief="b",
        rationale="r",
        confidence=0.99,
        targets=[
            ChangesetTarget(
                repo="janus-bonus",
                path="src/main/java/Handler.java",
                symbol="com.example.Handler.processPokerstarsTransaction",
            )
        ],
        metrics_delta={"needs_coverage": True, "coverage_index_empty": True},
    )
    needs, reason = changeset_needs_hitl(split, 0.6, queue=[split])
    assert needs is False
    assert reason != "uncovered_structural"

    ensure = Changeset(
        id="prep1",
        title="Ensure tests",
        action="ensure_tests",
        brief="b",
        rationale="r",
        confidence=0.55,
        targets=[],
        metrics_delta={"followup": True},
    )
    split_with_prep = split.model_copy(update={"depends_on": ["prep1"]})
    needs, reason = changeset_needs_hitl(
        split_with_prep, 0.6, deps_satisfied=False, queue=[split_with_prep, ensure]
    )
    assert needs is False
    assert reason == "blocked_deps"

    extract = Changeset(
        id="e4a2f1d26177efee",
        title="Extract shared across endpoints",
        action="extract_shared",
        brief="b",
        rationale="r",
        confidence=0.99,
        targets=[
            ChangesetTarget(repo="janus-account", path="a.java", symbol=f"com.example.S.m{i}")
            for i in range(6)
        ],
    )
    needs, reason = changeset_needs_hitl(extract, 0.6)
    assert needs is False
    assert reason != "blast_radius"


def test_janus_bonus_shape_autorun_and_waiting(tmp_path: Path) -> None:
    from autorefactor.changesets.store import ChangesetStore, changeset_needs_hitl
    from autorefactor.mcp.tools import _queue_counts

    preps = [
        Changeset(
            id=f"prep{i}",
            title=f"Ensure tests {i}",
            action="ensure_tests",
            brief="characterization coverage",
            rationale="prep",
            confidence=0.78 if i == 0 else 0.72,
            targets=[
                ChangesetTarget(
                    repo="janus-bonus",
                    path=f"src/main/java/H{i}.java",
                    symbol=f"com.example.H{i}",
                )
            ],
        )
        for i in range(9)
    ]
    splits = [
        Changeset(
            id="split-hi-99",
            title="Split high 0.99",
            action="split",
            brief="b",
            rationale="r",
            confidence=0.99,
            depends_on=["prep0"],
            targets=[
                ChangesetTarget(
                    repo="janus-bonus", path="src/main/java/A.java", symbol="A.m"
                )
            ],
        ),
        Changeset(
            id="split-hi-83",
            title="Split high 0.83",
            action="split",
            brief="b",
            rationale="r",
            confidence=0.83,
            depends_on=["prep1"],
            targets=[
                ChangesetTarget(
                    repo="janus-bonus", path="src/main/java/B.java", symbol="B.m"
                )
            ],
        ),
        Changeset(
            id="split-hi-73",
            title="Split high 0.73",
            action="split",
            brief="b",
            rationale="r",
            confidence=0.73,
            depends_on=["prep2"],
            targets=[
                ChangesetTarget(
                    repo="janus-bonus", path="src/main/java/C.java", symbol="C.m"
                )
            ],
        ),
        Changeset(
            id="split-lo-54",
            title="Split low 0.54",
            action="split",
            brief="b",
            rationale="r",
            confidence=0.54,
            depends_on=["prep3"],
            targets=[
                ChangesetTarget(
                    repo="janus-bonus", path="src/main/java/D.java", symbol="D.m"
                )
            ],
        ),
        Changeset(
            id="split-slop",
            title="Split ConfigurationValidator",
            action="split",
            brief="re-enable startup validation",
            rationale="disabled_startup_validation",
            confidence=0.79,
            depends_on=["prep4"],
            targets=[
                ChangesetTarget(
                    repo="janus-bonus",
                    path="src/main/java/ConfigurationValidator.java",
                    symbol="ConfigurationValidator",
                )
            ],
            metrics_delta={"kind": "disabled_startup_validation"},
        ),
        Changeset(
            id="extract-slop",
            title="Extract shared draft map",
            action="extract_shared",
            brief="draft in-memory mapping",
            rationale="draft_in_memory_map",
            confidence=0.76,
            depends_on=["prep5"],
            targets=[
                ChangesetTarget(
                    repo="janus-bonus",
                    path="src/main/java/MappingService.java",
                    symbol="MappingService",
                )
            ],
            metrics_delta={"kind": "draft_in_memory_map"},
        ),
    ]
    self_merge = Changeset(
        id="merge-self",
        title="Merge duplicate fill overloads",
        action="merge",
        brief="identical qualified symbol on both targets",
        rationale="dedup",
        confidence=0.99,
        targets=[
            ChangesetTarget(
                repo="janus-bonus", path="src/main/java/A.java", symbol="com.example.A.fill"
            ),
            ChangesetTarget(
                repo="janus-bonus", path="src/main/java/A.java", symbol="com.example.A.fill"
            ),
        ],
    )
    items = [*preps, *splits, self_merge]

    waiting_ids = {c.id for c in splits}
    for cs in items:
        deps_ok = not cs.depends_on
        needs, reason = changeset_needs_hitl(cs, 0.6, deps_satisfied=deps_ok, queue=items)
        if cs.id in waiting_ids:
            assert needs is False
            assert reason == "blocked_deps"
        elif cs.action == "ensure_tests":
            assert needs is False
        elif cs.id == "merge-self":
            assert needs is True
            assert reason == "self_merge"

    counts = _queue_counts(tmp_path / "out", threshold=0.6, items=items)
    assert counts["pending"] == len(items)
    assert counts["pending_waiting"] == len(splits)
    assert counts["pending_hitl"] == 1
    assert counts["pending_autorun"] == len(preps)
    assert counts["pending_hitl"] / counts["pending"] < 0.5

    store = ChangesetStore(tmp_path / "queue.json")
    store.save(items)
    nxt = store.next_pending(min_confidence=0.6)
    assert nxt.changeset is not None
    assert nxt.changeset.action == "ensure_tests"
    assert nxt.needs_confirmation is False

    done_preps = [
        c.model_copy(update={"status": ChangesetStatus.DONE}) for c in preps
    ]
    ready_items = [*done_preps, *splits, self_merge]
    for cid, expect_hitl, expect_reason in (
        ("split-hi-99", False, None),
        ("split-hi-83", False, None),
        ("split-hi-73", False, None),
        ("split-lo-54", True, "low_confidence"),
        ("split-slop", True, "slop_sensitive"),
        ("extract-slop", True, "slop_sensitive"),
        ("merge-self", True, "self_merge"),
    ):
        cs = next(c for c in ready_items if c.id == cid)
        needs, reason = changeset_needs_hitl(
            cs, 0.6, deps_satisfied=True, queue=ready_items
        )
        assert needs is expect_hitl, cid
        if expect_reason:
            assert reason == expect_reason, cid
        else:
            assert reason is None, cid

    ready_counts = _queue_counts(tmp_path / "out", threshold=0.6, items=ready_items)
    assert ready_counts["pending_waiting"] == 0
    assert ready_counts["pending_autorun"] == 3
    assert ready_counts["pending_hitl"] == 4

