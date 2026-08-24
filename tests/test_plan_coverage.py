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
    findings = [_finding(f"f{i}", "shared") for i in range(12)]
    findings[0] = findings[0].model_copy(update={"score": 0.85})
    for i in range(1, 12):
        findings[i] = findings[i].model_copy(update={"score": 0.2})

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
    assert findings[0].id in selected
    dropped = [c for c in confirmed if not c.valid]
    assert dropped
    assert all(c.finding.score < 0.6 for c in dropped)
    assert len(changesets) >= 1


def test_plan_llm_valid_false_drops_changeset() -> None:
    units = [
        CodeUnit(
            id="u1",
            repo="r",
            path="GetName.java",
            qualified_name="Bean.getName",
            kind=UnitKind.FUNCTION,
            language="java",
            source_snippet="public String getName() { return name; }",
            content_hash=content_hash("g"),
            span=CodeSpan(start_line=1, end_line=1),
        )
    ]
    finding = _finding("fp-1", "u1")
    finding = finding.model_copy(
        update={"score": 0.22, "title": "clone of getter", "evidence": "name similarity"}
    )

    class RejectStub(StubProvider):
        def chat_json(self, system: str, user: str, **kwargs):  # noqa: ANN003
            assert "snippet:" in user
            assert "getName" in user
            return {
                "decisions": [
                    {
                        "title": "false-positive getter",
                        "action": "split",
                        "rationale": "trivial getter",
                        "confidence": 0.1,
                        "finding_ids": [finding.id],
                        "unit_ids": ["u1"],
                        "valid": False,
                    }
                ]
            }

    confirmed, changesets, decisions, _agenda = plan_findings(
        [finding], units, RejectStub()
    )
    assert changesets == []
    assert decisions and decisions[0].valid is False
    assert all(not c.valid for c in confirmed)


def test_plan_brief_includes_source_snippet() -> None:
    units = [
        CodeUnit(
            id="u1",
            repo="r",
            path="A.java",
            qualified_name="A.foo",
            kind=UnitKind.FUNCTION,
            language="java",
            source_snippet="void foo() { return 42; }",
            content_hash=content_hash("s"),
            span=CodeSpan(start_line=1, end_line=1),
        )
    ]
    finding = _finding("s1", "u1").model_copy(update={"score": 0.21})
    seen: dict[str, str] = {}

    class CaptureStub(StubProvider):
        def chat_json(self, system: str, user: str, **kwargs):  # noqa: ANN003
            seen["system"] = system
            seen["user"] = user
            return {
                "decisions": [
                    {
                        "title": "keep",
                        "action": "split",
                        "rationale": "ok",
                        "confidence": 0.7,
                        "finding_ids": [finding.id],
                        "unit_ids": ["u1"],
                        "valid": True,
                    }
                ]
            }

    plan_findings([finding], units, CaptureStub())
    assert "snippet:" in seen["user"]
    assert "void foo() { return 42; }" in seen["user"]
    assert "disposition" in seen["system"].lower()
    assert "trash" in seen["system"].lower()



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
