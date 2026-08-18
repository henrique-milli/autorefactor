from __future__ import annotations

from autorefactor.agenda import conflict_components, split_oversized_components
from autorefactor.domain import (
    AnalysisGoal,
    CodeSpan,
    CodeUnit,
    Finding,
    Severity,
    SuggestedAction,
    UnitKind,
    content_hash,
)
from autorefactor.plan import plan_findings
from autorefactor.providers import StubProvider


def _finding(fid: str, uid: str, goal: AnalysisGoal = AnalysisGoal.STRUCTURE) -> Finding:
    return Finding(
        id=fid,
        goal=goal,
        sensor="structure",
        severity=Severity.MEDIUM,
        title=f"t-{fid}",
        evidence="e",
        unit_ids=[uid],
        action_hint=SuggestedAction.SPLIT,
        score=0.8,
    )


def test_split_oversized_components_by_goal_and_size() -> None:
    findings = [
        _finding(f"s{i}", f"u{i}", AnalysisGoal.STRUCTURE) for i in range(10)
    ] + [
        _finding(f"m{i}", f"u{i}", AnalysisGoal.METHOD_SIMILARITY) for i in range(5)
    ]
    # Force one mega component via shared unit
    mega = [_finding(f"x{i}", "shared", AnalysisGoal.STRUCTURE) for i in range(20)]
    for i, f in enumerate(mega):
        mega[i] = f.model_copy(update={"score": 1.0 - i * 0.01})
    comps = conflict_components(mega)
    assert len(comps) == 1 and len(comps[0]) == 20
    split = split_oversized_components(comps, max_size=8)
    assert all(len(c) <= 8 for c in split)
    assert sum(len(c) for c in split) == 20
    assert len(split) == 3  # 8+8+4


def test_plan_findings_covers_all_in_large_component() -> None:
    units = [
        CodeUnit(
            id="shared",
            repo="r",
            path="A.java",
            qualified_name="A",
            kind=UnitKind.TYPE,
            language="java",
            source_snippet="class A {}",
            content_hash=content_hash("a"),
            span=CodeSpan(start_line=1, end_line=1),
        )
    ]
    # Stub chat_json returns only first finding — fallback must cover the rest
    findings = [_finding(f"f{i}", "shared") for i in range(12)]

    class PartialStub(StubProvider):
        def chat_json(self, system: str, user: str, **kwargs):  # noqa: ANN003
            # Intentionally cover only one finding
            return {
                "decisions": [
                    {
                        "title": "only-one",
                        "action": "split",
                        "rationale": "partial",
                        "confidence": 0.9,
                        "finding_ids": [findings[0].id],
                        "unit_ids": ["shared"],
                        "valid": True,
                    }
                ]
            }

    confirmed, changesets, decisions, _agenda = plan_findings(
        findings, units, PartialStub(), max_component_size=8
    )
    selected = {c.finding.id for c in confirmed if c.valid}
    assert len(selected) == 12
    assert not any("Dropped during plan" in (c.reasoning or "") for c in confirmed)
    assert len(changesets) >= 1


def test_dedupe_changesets_keeps_higher_confidence() -> None:
    from autorefactor.changesets.models import Changeset, ChangesetTarget
    from autorefactor.plan.planner import _dedupe_changesets

    targets = [ChangesetTarget(repo="a", path="A.java", symbol="x")]
    a = Changeset(
        id="1",
        title="t",
        action="extract_shared",
        brief="b",
        rationale="r",
        confidence=0.5,
        targets=targets,
    )
    b = Changeset(
        id="2",
        title="t2",
        action="extract_shared",
        brief="b",
        rationale="r",
        confidence=0.9,
        targets=targets,
    )
    out = _dedupe_changesets([a, b])
    assert len(out) == 1
    assert out[0].id == "2"
