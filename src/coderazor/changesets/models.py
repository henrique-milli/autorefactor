from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class ChangesetStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    SKIPPED = "skipped"
    BLOCKED = "blocked"


class ChangesetTarget(BaseModel):
    repo: str
    path: str
    symbol: str | None = None
    unit_id: str | None = None


class Changeset(BaseModel):
    id: str
    title: str
    action: str
    brief: str
    rationale: str
    confidence: float = 0.5
    targets: list[ChangesetTarget] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    parallel_group: str | None = None
    acceptance: str = "Build and tests still pass for touched packages."
    metrics_delta: dict[str, Any] = Field(default_factory=dict)
    status: ChangesetStatus = ChangesetStatus.PENDING
    evidence_refs: list[str] = Field(default_factory=list)
    notes: str = ""


class NextChangesetResult(BaseModel):
    changeset: Changeset | None = None
    needs_confirmation: bool = False
    questions: list[str] = Field(default_factory=list)
    done: bool = False
