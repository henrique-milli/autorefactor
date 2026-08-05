from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class UnitKind(str, Enum):
    REPO = "repo"
    MODULE = "module"
    FILE = "file"
    TYPE = "type"
    FUNCTION = "function"
    PROTO_MESSAGE = "proto_message"
    PROTO_RPC = "proto_rpc"
    PROTO_SERVICE = "proto_service"


class FlagSource(str, Enum):
    SMELL = "smell"
    ANALYZER = "analyzer"
    SIMILARITY = "similarity"
    CLUSTER = "cluster"
    COVERAGE = "coverage"


class SuggestedAction(str, Enum):
    ENSURE_TESTS = "ensure_tests"
    MERGE = "merge"
    MOVE = "move"
    EXTRACT_SHARED = "extract_shared"
    SPLIT = "split"
    DELETE_DUPLICATE = "delete_duplicate"
    DOCUMENT = "document"


class Severity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class CodeSpan(BaseModel):
    start_line: int = 0
    end_line: int = 0
    start_byte: int = 0
    end_byte: int = 0


class CodeUnit(BaseModel):
    id: str
    repo: str
    path: str
    qualified_name: str
    kind: UnitKind
    language: str
    source_snippet: str
    content_hash: str
    parent_id: str | None = None
    span: CodeSpan = Field(default_factory=CodeSpan)
    description: str | None = None
    tags: list[str] = Field(default_factory=list)
    embedding: list[float] | None = None

    def describe_text(self) -> str:
        return self.description or self.source_snippet[:500]


class Flag(BaseModel):
    id: str
    source: FlagSource
    severity: Severity
    title: str
    evidence: str
    unit_ids: list[str] = Field(default_factory=list)
    suggested_action: SuggestedAction = SuggestedAction.DOCUMENT
    score: float = 0.0
    metadata: dict[str, Any] = Field(default_factory=dict)


class ConfirmedFinding(BaseModel):
    flag: Flag
    valid: bool
    reasoning: str
    refined_action: SuggestedAction | None = None


class PlanStep(BaseModel):
    step_id: str
    title: str
    action: SuggestedAction
    finding_ids: list[str] = Field(default_factory=list)
    unit_ids: list[str] = Field(default_factory=list)
    target_paths: list[str] = Field(default_factory=list)
    blast_radius: str = "medium"
    acceptance: str = "Tests and builds still pass."
    rationale: str = ""


class RefactorPlan(BaseModel):
    summary: str
    steps: list[PlanStep] = Field(default_factory=list)
    confirmed_count: int = 0
    rejected_count: int = 0
    tests_required: bool = True


class TestGap(BaseModel):
    unit_id: str
    repo: str
    path: str
    qualified_name: str
    language: str
    reason: str
    suggested_test_path: str
    finding_ids: list[str] = Field(default_factory=list)


class TestPlan(BaseModel):
    summary: str
    gaps: list[TestGap] = Field(default_factory=list)
    existing_test_files: list[str] = Field(default_factory=list)
    recommended_cmd: str | None = None


class TestHarnessResult(BaseModel):
    passed: bool
    summary: str
    created_files: list[str] = Field(default_factory=list)
    test_output: str = ""
    skipped: bool = False


class DescriptionResult(BaseModel):
    description: str
    tags: list[str] = Field(default_factory=list)


class RunManifest(BaseModel):
    created_at: str
    workspace: str
    repos_scanned: list[str]
    unit_count: int
    flag_count: int = 0
    confirmed_count: int = 0
    llm_model: str
    embed_model: str
    similarity_threshold: float
    out_dir: str = ""


class DocumentGap(BaseModel):
    unit_id: str
    repo: str
    path: str
    qualified_name: str
    language: str
    kind: str
    reason: str = "Missing in-source documentation"


class DocumentReport(BaseModel):
    summary: str
    gaps: list[DocumentGap] = Field(default_factory=list)
    touched_paths: list[str] = Field(default_factory=list)
    skipped: bool = False


class CoverageFileStat(BaseModel):
    path: str
    covered_lines: int = 0
    total_lines: int = 0
    percent: float = 0.0


class CoverageSnapshot(BaseModel):
    summary: str = ""
    overall_percent: float | None = None
    files: list[CoverageFileStat] = Field(default_factory=list)
    source: str = "unknown"


class AnalyzerRunNote(BaseModel):
    name: str
    status: str  # ran | skipped | error
    detail: str = ""
    flag_count: int = 0


class AnalysisArtifacts(BaseModel):
    """Aggregate view across stage-owned artifact directories."""

    units: list[CodeUnit] = Field(default_factory=list)
    flags: list[Flag] = Field(default_factory=list)
    findings: list[ConfirmedFinding] = Field(default_factory=list)
    plan: RefactorPlan | None = None
    test_plan: TestPlan | None = None
    test_harness: TestHarnessResult | None = None
    manifest: RunManifest | None = None
    document_report: DocumentReport | None = None
    coverage: CoverageSnapshot | None = None
    analyzer_notes: list[AnalyzerRunNote] = Field(default_factory=list)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def unit_id(*parts: str) -> str:
    import hashlib

    return hashlib.sha256(":".join(parts).encode("utf-8")).hexdigest()


def content_hash(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()
