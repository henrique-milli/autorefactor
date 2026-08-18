"""Changeset queue helpers — display limit never drops persisted work."""

from __future__ import annotations

from pathlib import Path

from autorefactor.changesets.models import Changeset, NextChangesetResult
from autorefactor.changesets.store import ChangesetStore


def save_queue(out_dir: Path, changesets: list[Changeset]) -> Path:
    """Write v2 queue path and compat copy under changesets/queue.json."""
    v2 = out_dir / "queue" / "changesets.json"
    compat = out_dir / "changesets" / "queue.json"
    ChangesetStore(v2).save(changesets)
    ChangesetStore(compat).save(changesets)
    return v2


def load_queue(out_dir: Path) -> list[Changeset]:
    v2 = out_dir / "queue" / "changesets.json"
    compat = out_dir / "changesets" / "queue.json"
    if v2.exists():
        return ChangesetStore(v2).load()
    return ChangesetStore(compat).load()


def next_for_display(
    out_dir: Path,
    *,
    min_confidence: float = 0.0,
    display_limit: int | None = None,
) -> NextChangesetResult:
    """Return next pending changeset. ``display_limit`` only affects scanning order window."""
    store_path = out_dir / "queue" / "changesets.json"
    if not store_path.exists():
        store_path = out_dir / "changesets" / "queue.json"
    store = ChangesetStore(store_path)
    items = store.load()
    if display_limit is not None and display_limit > 0:
        # Only consider the top-N by confidence among pending for "next" UX
        from autorefactor.changesets.models import ChangesetStatus

        pending = [
            c
            for c in items
            if c.status in {ChangesetStatus.PENDING, ChangesetStatus.IN_PROGRESS}
        ]
        pending.sort(key=lambda c: -c.confidence)
        window_ids = {c.id for c in pending[:display_limit]}
        # Temporarily filter by rewriting store view
        filtered = [c for c in items if c.id in window_ids or c.status not in {
            ChangesetStatus.PENDING, ChangesetStatus.IN_PROGRESS
        }]
        tmp = ChangesetStore(store_path)
        # Use in-memory next without rewriting disk
        class _View(ChangesetStore):
            def load(self_inner):  # noqa: N805
                return filtered

        return _View(store_path).next_pending(min_confidence=min_confidence)
    return store.next_pending(min_confidence=min_confidence)
