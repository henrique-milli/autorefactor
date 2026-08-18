from __future__ import annotations

from autorefactor.domain import (
    AnalysisGoal,
    CodeSpan,
    CodeUnit,
    ConfirmedFinding,
    Finding,
    Severity,
    SuggestedAction,
    UnitKind,
    content_hash,
    unit_id,
)
from autorefactor.testing.gaps import find_test_gaps, suggested_test_path


def _unit(
    uid: str,
    qname: str,
    kind: UnitKind,
    *,
    parent_id: str | None = None,
) -> CodeUnit:
    simple = qname.rsplit(".", 1)[-1]
    return CodeUnit(
        id=uid,
        repo="alpha",
        path="alpha/util.py",
        qualified_name=qname,
        kind=kind,
        language="python",
        source_snippet=f"class {simple}: pass" if kind == UnitKind.TYPE else "def x(): pass",
        content_hash=content_hash(uid),
        parent_id=parent_id,
        span=CodeSpan(start_line=1, end_line=5),
    )


def test_score_aggregator_methods_collapse_to_one_gap() -> None:
    type_unit = _unit(unit_id("t"), "ScoreAggregator", UnitKind.TYPE)
    add = _unit(unit_id("a"), "ScoreAggregator.add", UnitKind.FUNCTION, parent_id=type_unit.id)
    snap = _unit(unit_id("s"), "ScoreAggregator.snapshot", UnitKind.FUNCTION, parent_id=type_unit.id)
    units = [type_unit, add, snap]

    assert suggested_test_path(add) == suggested_test_path(snap) == suggested_test_path(type_unit)

    flag = Finding(
        id=content_hash("f"),
        goal=AnalysisGoal.STRUCTURE,
        sensor="structure",
        severity=Severity.MEDIUM,
        title="dup",
        evidence="e",
        unit_ids=[add.id, snap.id],
        action_hint=SuggestedAction.EXTRACT_SHARED,
        score=0.9,
    )
    finding = ConfirmedFinding(finding=flag, valid=True, reasoning="ok")
    gaps = find_test_gaps([finding], units, existing_tests=[])
    assert len(gaps) == 1
    assert gaps[0].suggested_test_path.endswith("test_alpha_util_ScoreAggregator.py")
    assert gaps[0].unit_id == type_unit.id
