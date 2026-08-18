from __future__ import annotations

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
from autorefactor.sensors.validate import structural_reason, validate_findings


def _unit(key: str, *, path: str, qn: str, kind: UnitKind = UnitKind.FUNCTION) -> CodeUnit:
    return CodeUnit(
        id=unit_id(key),
        repo="demo",
        path=path,
        qualified_name=qn,
        kind=kind,
        language="java",
        source_snippet=f"void {qn}() {{}}\n" * 3,
        content_hash=content_hash(key),
        span=CodeSpan(start_line=1, end_line=5),
    )


def test_move_not_structurally_banned() -> None:
    a = _unit("a", path="A.java", qn="A.foo")
    b = _unit("b", path="B.java", qn="B")
    f = Finding(
        id="1",
        goal=AnalysisGoal.PLACEMENT,
        sensor="placement",
        severity=Severity.HIGH,
        title="move",
        evidence="e",
        unit_ids=[a.id, b.id],
        action_hint=SuggestedAction.MOVE,
    )
    assert structural_reason(f, {a.id, b.id}) is None


def test_empty_targets_rejected() -> None:
    f = Finding(
        id="2",
        goal=AnalysisGoal.STRUCTURE,
        sensor="structure",
        severity=Severity.LOW,
        title="empty",
        evidence="e",
        unit_ids=[],
        action_hint=SuggestedAction.SPLIT,
    )
    assert structural_reason(f, set()) == "empty_targets"


def test_validate_keeps_moves() -> None:
    a = _unit("a", path="A.java", qn="A.foo")
    b = _unit("b", path="B.java", qn="B.bar")
    f = Finding(
        id="m",
        goal=AnalysisGoal.PLACEMENT,
        sensor="placement",
        severity=Severity.HIGH,
        title="move",
        evidence="e",
        unit_ids=[a.id, b.id],
        action_hint=SuggestedAction.MOVE,
        score=0.9,
    )
    kept, rejected = validate_findings([f], {a.id, b.id})
    assert len(kept) == 1
    assert rejected == []
