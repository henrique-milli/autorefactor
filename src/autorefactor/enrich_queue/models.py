from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class EnrichTaskStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    SKIPPED = "skipped"
    BLOCKED = "blocked"


class EnrichTaskKind(str, Enum):
    DESCRIBE = "describe"
    PLAN = "plan"


class EnrichTask(BaseModel):
    id: str
    kind: EnrichTaskKind
    payload: dict[str, Any] = Field(default_factory=dict)
    status: EnrichTaskStatus = EnrichTaskStatus.PENDING
    schema_version: int = 1
    result: dict[str, Any] | None = None
    notes: str = ""
    priority: int = 0


class DescribeEnrichResult(BaseModel):
    """Expected result for kind=describe."""

    doc: str = ""
    description: str = ""
    tags: list[str] = Field(default_factory=list)

    def normalized_doc(self) -> str:
        return (self.doc or self.description or "").strip()


class PlanDecisionResult(BaseModel):
    title: str
    action: str
    rationale: str = ""
    confidence: float = 0.5
    finding_ids: list[str] = Field(default_factory=list)
    unit_ids: list[str] = Field(default_factory=list)
    valid: bool = True


class PlanEnrichResult(BaseModel):
    """Expected result for kind=plan — one or more decisions for a component."""

    decisions: list[PlanDecisionResult] = Field(default_factory=list)


class NextEnrichResult(BaseModel):
    task: EnrichTask | None = None
    done: bool = False
