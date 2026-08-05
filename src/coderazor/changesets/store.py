from __future__ import annotations

import json
from pathlib import Path

from coderazor.changesets.models import Changeset, ChangesetStatus, NextChangesetResult


class ChangesetStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> list[Changeset]:
        if not self.path.exists():
            return []
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        items = raw.get("changesets", raw if isinstance(raw, list) else [])
        return [Changeset.model_validate(item) for item in items]

    def save(self, changesets: list[Changeset]) -> None:
        payload = {
            "changesets": [c.model_dump(mode="json") for c in changesets],
        }
        self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def list(self) -> list[Changeset]:
        return self.load()

    def get(self, changeset_id: str) -> Changeset | None:
        for item in self.load():
            if item.id == changeset_id:
                return item
        return None

    def mark(self, changeset_id: str, status: ChangesetStatus | str, notes: str = "") -> Changeset:
        items = self.load()
        status_enum = ChangesetStatus(status) if isinstance(status, str) else status
        for idx, item in enumerate(items):
            if item.id == changeset_id:
                updated = item.model_copy(
                    update={"status": status_enum, "notes": notes or item.notes}
                )
                items[idx] = updated
                self.save(items)
                return updated
        raise KeyError(f"changeset not found: {changeset_id}")

    def next_pending(self, *, min_confidence: float = 0.0) -> NextChangesetResult:
        pending = [
            c
            for c in self.load()
            if c.status in {ChangesetStatus.PENDING, ChangesetStatus.IN_PROGRESS}
        ]
        if not pending:
            return NextChangesetResult(done=True)
        # Prefer in_progress, then highest confidence among independent (no unmet depends)
        ids_done = {
            c.id
            for c in self.load()
            if c.status in {ChangesetStatus.DONE, ChangesetStatus.SKIPPED}
        }
        ready = [
            c
            for c in pending
            if all(dep in ids_done for dep in c.depends_on)
        ] or pending
        ready.sort(key=lambda c: (-(1 if c.status == ChangesetStatus.IN_PROGRESS else 0), -c.confidence))
        chosen = ready[0]
        if chosen.confidence < min_confidence:
            return NextChangesetResult(
                changeset=chosen,
                needs_confirmation=True,
                questions=[
                    f"Confidence {chosen.confidence:.2f} is below threshold {min_confidence:.2f}.",
                    f"Proceed with '{chosen.title}' ({chosen.action})?",
                    "Reply yes to continue, skip to skip, or provide guidance.",
                ],
            )
        return NextChangesetResult(changeset=chosen)
