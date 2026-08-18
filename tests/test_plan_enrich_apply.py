from __future__ import annotations

from pathlib import Path

from autorefactor.changesets.models import Changeset, ChangesetStatus, ChangesetTarget
from autorefactor.enrich_queue.apply_plan import apply_plan_enrich_to_queue
from autorefactor.queue import load_queue, save_queue


def _cs(
    *,
    id: str,
    confidence: float = 0.9,
    evidence_refs: list[str] | None = None,
    unit_ids: list[str] | None = None,
    parent: str | None = None,
    action: str = "split",
) -> Changeset:
    targets = [
        ChangesetTarget(repo="r", path="a.py", symbol="f", unit_id=uid)
        for uid in (unit_ids or [])
    ]
    md: dict = {}
    if parent:
        md["parent_changeset_id"] = parent
    return Changeset(
        id=id,
        title=f"title-{id}",
        action=action,
        brief="brief",
        rationale="old rationale",
        confidence=confidence,
        targets=targets,
        evidence_refs=list(evidence_refs or []),
        metrics_delta=md,
    )


def test_reject_skips_by_evidence_refs(tmp_path: Path) -> None:
    out = tmp_path / "out"
    save_queue(
        out,
        [
            _cs(id="cs1", evidence_refs=["f1"]),
            _cs(id="cs2", evidence_refs=["f2"]),
        ],
    )
    stats = apply_plan_enrich_to_queue(
        out,
        [
            {
                "title": "reject f1",
                "action": "merge",
                "rationale": "interface vs impl",
                "confidence": 0.2,
                "finding_ids": ["f1"],
                "unit_ids": [],
                "valid": False,
            }
        ],
    )
    assert stats["skipped"] == ["cs1"]
    assert stats["updated"] == []
    items = {c.id: c for c in load_queue(out)}
    assert items["cs1"].status == ChangesetStatus.SKIPPED
    assert "plan_enrich_rejected" in items["cs1"].notes
    assert "interface vs impl" in items["cs1"].notes
    assert items["cs2"].status == ChangesetStatus.PENDING


def test_accept_updates_confidence_and_action(tmp_path: Path) -> None:
    out = tmp_path / "out"
    save_queue(out, [_cs(id="cs1", evidence_refs=["f1"], action="split", confidence=0.7)])
    stats = apply_plan_enrich_to_queue(
        out,
        [
            {
                "title": "better title",
                "action": "extract_shared",
                "rationale": "shared helper",
                "confidence": 0.92,
                "finding_ids": ["f1"],
                "unit_ids": [],
                "valid": True,
            }
        ],
    )
    assert stats["updated"] == ["cs1"]
    item = load_queue(out)[0]
    assert item.action == "extract_shared"
    assert item.confidence == 0.92
    assert item.rationale == "shared helper"
    assert item.brief == "shared helper"
    assert item.title == "better title"
    assert item.status == ChangesetStatus.PENDING


def test_unit_id_fallback_when_no_finding_ids(tmp_path: Path) -> None:
    out = tmp_path / "out"
    save_queue(out, [_cs(id="cs1", unit_ids=["u1"], evidence_refs=[])])
    stats = apply_plan_enrich_to_queue(
        out,
        [
            {
                "title": "t",
                "action": "document",
                "rationale": "orphan",
                "confidence": 0.5,
                "finding_ids": [],
                "unit_ids": ["u1"],
                "valid": False,
            }
        ],
    )
    assert stats["skipped"] == ["cs1"]


def test_parent_skip_cascades_to_followup(tmp_path: Path) -> None:
    out = tmp_path / "out"
    save_queue(
        out,
        [
            _cs(id="parent", evidence_refs=["f1"]),
            _cs(id="child", evidence_refs=["f9"], parent="parent"),
        ],
    )
    stats = apply_plan_enrich_to_queue(
        out,
        [
            {
                "title": "reject",
                "action": "split",
                "rationale": "fp",
                "confidence": 0.1,
                "finding_ids": ["f1"],
                "unit_ids": [],
                "valid": False,
            }
        ],
    )
    assert "parent" in stats["skipped"]
    assert "child" in stats["skipped"]
    items = {c.id: c for c in load_queue(out)}
    assert items["parent"].status == ChangesetStatus.SKIPPED
    assert items["child"].status == ChangesetStatus.SKIPPED
    assert "parent_skipped" in items["child"].notes


def test_unmatched_decision_listed(tmp_path: Path) -> None:
    out = tmp_path / "out"
    save_queue(out, [_cs(id="cs1", evidence_refs=["f1"])])
    stats = apply_plan_enrich_to_queue(
        out,
        [
            {
                "title": "orphan decision",
                "action": "merge",
                "rationale": "x",
                "confidence": 0.8,
                "finding_ids": ["nope"],
                "unit_ids": [],
                "valid": True,
            }
        ],
    )
    assert stats["unmatched"] == ["orphan decision"]
    assert load_queue(out)[0].status == ChangesetStatus.PENDING
