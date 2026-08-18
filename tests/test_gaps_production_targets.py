from __future__ import annotations

from autorefactor.domain import (
    AnalysisGoal,
    CodeSpan,
    CodeUnit,
    ConfirmedFinding,
    CoverageSnapshot,
    Finding,
    Severity,
    SuggestedAction,
    UnitKind,
    content_hash,
)
from autorefactor.testing.gaps import (
    find_test_gaps,
    is_coverage_padding_path,
    resolve_production_units_for_ensure_tests,
)


def _java_unit(
    uid: str,
    *,
    path: str,
    qname: str,
    kind: UnitKind = UnitKind.TYPE,
) -> CodeUnit:
    return CodeUnit(
        id=uid,
        repo="janus-bonus",
        path=path,
        qualified_name=qname,
        kind=kind,
        language="java",
        source_snippet=f"class {qname.rsplit('.', 1)[-1]} {{}}",
        content_hash=content_hash(uid),
        span=CodeSpan(start_line=1, end_line=10),
    )


def test_is_coverage_padding_path_detects_bonus_model_coverage_test() -> None:
    p = "janus-bonus/src/test/java/com/example/BonusModelCoverageTest.java"
    assert is_coverage_padding_path(p)


def test_padding_finding_resolves_to_production_service_not_test_file() -> None:
    padding = _java_unit(
        "pad",
        path="janus-bonus/src/test/java/com/example/BonusModelCoverageTest.java",
        qname="com.example.BonusModelCoverageTest",
    )
    pam = _java_unit(
        "pam",
        path="janus-bonus/src/main/java/com/example/v1/client/PamBonusClientImpl.java",
        qname="com.example.v1.client.PamBonusClientImpl",
    )
    bonus_svc = _java_unit(
        "svc",
        path="janus-bonus/src/main/java/com/example/v1/service/BonusServiceImpl.java",
        qname="com.example.v1.service.BonusServiceImpl",
    )
    units = [padding, pam, bonus_svc]
    flag = Finding(
        id=content_hash("f"),
        goal=AnalysisGoal.COVERAGE,
        sensor="test_quality",
        severity=Severity.HIGH,
        title="Jacoco padding",
        evidence="BonusModelCoverageTest getter/setter padding",
        unit_ids=[padding.id],
        action_hint=SuggestedAction.ENSURE_TESTS,
        score=0.8,
        metadata={"kind": "coverage_padding"},
    )
    gaps = find_test_gaps([ConfirmedFinding(finding=flag, valid=True, reasoning="ok")], units, [])
    assert gaps
    paths = {g.path for g in gaps}
    assert padding.path not in paths
    assert any("src/main/" in p for p in paths)
    qnames = {g.qualified_name for g in gaps}
    assert any("PamBonusClientImpl" in q or "BonusServiceImpl" in q for q in qnames)


def test_resolve_production_units_prefers_low_coverage_main_sources() -> None:
    low = _java_unit(
        "low",
        path="janus-bonus/src/main/java/com/example/v1/client/PamBonusClientImpl.java",
        qname="com.example.v1.client.PamBonusClientImpl",
    )
    padding = _java_unit(
        "pad",
        path="janus-bonus/src/test/java/com/example/BonusModelCoverageTest.java",
        qname="com.example.BonusModelCoverageTest",
    )
    coverage = CoverageSnapshot(
        summary="jacoco",
        source="jacoco",
        files=[],
    )
    resolved = resolve_production_units_for_ensure_tests(
        [padding],
        [low, padding],
        coverage=coverage,
        evidence_text="PamBonusClientImpl retrieveBonus error branch",
    )
    assert resolved
    assert resolved[0].path == low.path
    assert "PamBonusClientImpl" in resolved[0].qualified_name


def test_mock_only_resolves_multiple_production_targets_not_exception() -> None:
    repo = _java_unit(
        "repo",
        path="janus-bonus",
        qname="janus-bonus",
        kind=UnitKind.REPO,
    )
    exc = _java_unit(
        "exc",
        path="janus-bonus/src/main/java/com/example/PamClientException.java",
        qname="com.example.PamClientException",
    )
    svc = _java_unit(
        "svc",
        path="janus-bonus/src/main/java/com/example/v1/service/BonusServiceImpl.java",
        qname="com.example.v1.service.BonusServiceImpl",
    )
    ctrl = _java_unit(
        "ctrl",
        path="janus-bonus/src/main/java/com/example/v1/controller/BonusController.java",
        qname="com.example.v1.controller.BonusController",
    )
    web_test = CodeUnit(
        id="web",
        repo="janus-bonus",
        path="janus-bonus/src/test/java/com/example/v1/controller/BonusControllerTest.java",
        qualified_name="com.example.v1.controller.BonusControllerTest",
        kind=UnitKind.TYPE,
        language="java",
        source_snippet="@WebMvcTest(BonusController.class)\nclass BonusControllerTest {}",
        content_hash=content_hash("web"),
        span=CodeSpan(start_line=1, end_line=10),
    )
    svc_test = CodeUnit(
        id="svctest",
        repo="janus-bonus",
        path="janus-bonus/src/test/java/com/example/v1/service/BonusServiceImplTest.java",
        qualified_name="com.example.v1.service.BonusServiceImplTest",
        kind=UnitKind.TYPE,
        language="java",
        source_snippet="class BonusServiceImplTest {}",
        content_hash=content_hash("svctest"),
        span=CodeSpan(start_line=1, end_line=10),
    )
    units = [repo, exc, svc, ctrl, web_test, svc_test]
    resolved = resolve_production_units_for_ensure_tests(
        [repo],
        units,
        evidence_text="WebMvcTest/Mockito smoke only",
        prep_kind="java_mock_only",
    )
    qnames = {u.qualified_name for u in resolved}
    assert not any("PamClientException" in q for q in qnames)
    assert any("BonusServiceImpl" in q for q in qnames)
    assert any("BonusController" in q for q in qnames)
    assert len(resolved) >= 2
