"""Tests for analyze-vs-realized queue progress."""

from __future__ import annotations

from autorefactor.changesets.models import Changeset, ChangesetStatus, ChangesetTarget
from autorefactor.report.progress import compute_queue_progress


def _cs(
    cid: str,
    *,
    status: ChangesetStatus,
    action: str = "merge",
    rem: int = 10,
    rev: int = 20,
    conf: float = 0.9,
) -> Changeset:
    return Changeset(
        id=cid,
        title=cid,
        action=action,
        brief="b",
        rationale="r",
        confidence=conf,
        targets=[ChangesetTarget(repo="demo", path="a.py", unit_id=f"u-{cid}")],
        metrics_delta={"estimated_loc_removed": rem, "loc_under_review": rev},
        status=status,
    )


def test_progress_splits_done_and_remaining() -> None:
    metrics = {
        "roi": {
            "debt_score": {"value": 60.0, "components": {"complexity": 40, "clone": 50}},
            "agent_friction": {"value": 55.0},
        },
        "impact": {
            "estimated_loc_removed": 30,
            "loc_under_review": 60,
            "agent_ready_changesets": 3,
            "clone_paths_at_risk": 2,
        },
        "exact": {"slop_score": 0, "flags_by_goal": {"structure": 2, "method_similarity": 1}},
    }
    changesets = [
        _cs("a", status=ChangesetStatus.DONE, rem=10, rev=20),
        _cs("b", status=ChangesetStatus.PENDING, rem=10, rev=20),
        _cs("c", status=ChangesetStatus.SKIPPED, rem=10, rev=20),
    ]
    prog = compute_queue_progress(metrics, changesets)
    assert prog["counts"]["done"] == 1
    assert prog["counts"]["open"] == 1
    assert prog["counts"]["skipped"] == 1
    assert prog["at_analyze"]["estimated_loc_removed"] == 30
    assert prog["at_analyze"]["debt_score"] == 60.0
    assert prog["realized"]["estimated_loc_removed"] == 10
    assert prog["realized"]["tasks"] == 1
    assert prog["remaining"]["estimated_loc_removed"] == 10
    assert prog["remaining"]["tasks"] == 1
    assert prog["remaining"]["debt_score"] is not None
    assert prog["remaining"]["debt_score"] <= prog["at_analyze"]["debt_score"]
    assert prog["realized"]["debt_score_drop"] is not None
    assert prog["realized"]["debt_score_drop"] >= 0


def test_progress_all_done_clears_remaining_debt() -> None:
    metrics = {
        "roi": {"debt_score": {"value": 40.0}, "agent_friction": {"value": 30.0}},
        "impact": {"estimated_loc_removed": 10, "loc_under_review": 10, "agent_ready_changesets": 1},
        "exact": {"flags_by_goal": {}},
    }
    changesets = [_cs("a", status=ChangesetStatus.DONE, rem=10)]
    prog = compute_queue_progress(metrics, changesets)
    assert prog["remaining"]["tasks"] == 0
    assert prog["remaining"]["debt_score"] == 0.0
    assert prog["remaining"]["agent_friction"] == 0.0
    assert prog["realized"]["debt_score_drop"] == 40.0
