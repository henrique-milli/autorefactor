from __future__ import annotations

from pathlib import Path

from autorefactor.changesets.models import Changeset, ChangesetStatus, ChangesetTarget
from autorefactor.changesets.store import ChangesetStore


def test_next_pending_prefers_cross_repo(tmp_path: Path) -> None:
    store = ChangesetStore(tmp_path / "queue.json")
    local = Changeset(
        id="local",
        title="local",
        action="split",
        brief="b",
        rationale="r",
        confidence=0.99,
        targets=[ChangesetTarget(repo="a", path="A.java")],
        metrics_delta={"cross_repo": False},
        status=ChangesetStatus.PENDING,
    )
    cross = Changeset(
        id="cross",
        title="cross",
        action="extract_shared",
        brief="b",
        rationale="r",
        confidence=0.7,
        targets=[
            ChangesetTarget(repo="a", path="A.java"),
            ChangesetTarget(repo="b", path="B.java"),
        ],
        metrics_delta={"cross_repo": True},
        status=ChangesetStatus.PENDING,
    )
    store.save([local, cross])
    nxt = store.next_pending()
    assert nxt.changeset is not None
    assert nxt.changeset.id == "cross"
