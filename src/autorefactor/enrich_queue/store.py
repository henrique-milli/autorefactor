from __future__ import annotations

import json
from pathlib import Path

from autorefactor.enrich_queue.models import (
    DescribeEnrichResult,
    EnrichTask,
    EnrichTaskKind,
    EnrichTaskStatus,
    NextEnrichResult,
    PlanEnrichResult,
)


class EnrichTaskStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> list[EnrichTask]:
        if not self.path.exists():
            return []
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        items = raw.get("enrich_tasks", raw if isinstance(raw, list) else [])
        return [EnrichTask.model_validate(item) for item in items]

    def save(self, tasks: list[EnrichTask]) -> None:
        payload = {"enrich_tasks": [t.model_dump(mode="json") for t in tasks]}
        self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def list(self) -> list[EnrichTask]:
        return self.load()

    def get(self, task_id: str) -> EnrichTask | None:
        for item in self.load():
            if item.id == task_id:
                return item
        return None

    def replace_all(self, tasks: list[EnrichTask]) -> None:
        self.save(tasks)

    def mark(
        self,
        task_id: str,
        status: EnrichTaskStatus | str,
        *,
        notes: str = "",
        result: dict | None = None,
    ) -> EnrichTask:
        items = self.load()
        status_enum = EnrichTaskStatus(status) if isinstance(status, str) else status
        for idx, item in enumerate(items):
            if item.id == task_id:
                updates: dict = {"status": status_enum}
                if notes:
                    updates["notes"] = notes
                if result is not None:
                    updates["result"] = result
                updated = item.model_copy(update=updates)
                items[idx] = updated
                self.save(items)
                return updated
        raise KeyError(f"enrich task not found: {task_id}")

    def next_pending(self) -> NextEnrichResult:
        pending = [
            t
            for t in self.load()
            if t.status in {EnrichTaskStatus.PENDING, EnrichTaskStatus.IN_PROGRESS}
        ]
        if not pending:
            return NextEnrichResult(done=True)
        pending.sort(
            key=lambda t: (
                -(1 if t.status == EnrichTaskStatus.IN_PROGRESS else 0),
                -(1 if t.kind == EnrichTaskKind.DESCRIBE else 0),
                -t.priority,
                t.id,
            )
        )
        return NextEnrichResult(task=pending[0])


def validate_enrich_result(kind: EnrichTaskKind | str, result: dict) -> dict:
    """Validate and normalize agent-submitted enrich results. Raises ValueError on bad shape."""
    kind_enum = EnrichTaskKind(kind) if isinstance(kind, str) else kind
    if kind_enum == EnrichTaskKind.DESCRIBE:
        parsed = DescribeEnrichResult.model_validate(result)
        doc = parsed.normalized_doc()
        if not doc:
            raise ValueError("describe result requires non-empty doc or description")
        return {"doc": doc, "description": doc, "tags": list(parsed.tags)}
    if kind_enum == EnrichTaskKind.PLAN:
        parsed = PlanEnrichResult.model_validate(result)
        if not parsed.decisions:
            raise ValueError("plan result requires at least one decision")
        return parsed.model_dump(mode="json")
    raise ValueError(f"unknown enrich kind: {kind}")


def enrich_queue_path(out_dir: Path) -> Path:
    return out_dir / "queue" / "enrich_tasks.json"
