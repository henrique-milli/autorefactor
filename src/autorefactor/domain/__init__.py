"""Autorefactor 0.2 domain types — single taxonomy for evidence → agenda → decisions."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = 2

PlanDisposition = Literal["keep", "review", "trash"]
_PLAN_DISPOSITIONS = frozenset({"keep", "review", "trash"})


class UnitKind(str, Enum):
    REPO = "repo"
    MODULE = "module"
    FILE = "file"
    TYPE = "type"
    FUNCTION = "function"
    PROTO_MESSAGE = "proto_message"
    PROTO_RPC = "proto_rpc"
    PROTO_SERVICE = "proto_service"
    SQL_TABLE = "sql_table"
    SQL_POLICY = "sql_policy"
    SQL_FUNCTION = "sql_function"
    SQL_TRIGGER = "sql_trigger"
    SQL_INDEX = "sql_index"


class AnalysisGoal(str, Enum):
    METHOD_SIMILARITY = "method_similarity"
    CLASS_SIMILARITY = "class_similarity"
    STRUCTURAL_CLONE = "structural_clone"
    PLACEMENT = "placement"
    STRUCTURE = "structure"
    STATIC = "static"
    COVERAGE = "coverage"
    CLUSTER = "cluster"
    SLOP = "slop"


# Focus → enabled goals (no orchestrator side-effects)
FOCUS_GOALS: dict[str, frozenset[AnalysisGoal]] = {
    "balanced": frozenset(AnalysisGoal),
    "cross_repo": frozenset(
        {
            AnalysisGoal.METHOD_SIMILARITY,
            AnalysisGoal.CLASS_SIMILARITY,
            AnalysisGoal.STRUCTURAL_CLONE,
            AnalysisGoal.CLUSTER,
            AnalysisGoal.STATIC,
            AnalysisGoal.COVERAGE,
            AnalysisGoal.STRUCTURE,
            AnalysisGoal.SLOP,
        }
    ),
    "clones": frozenset(
        {
            AnalysisGoal.METHOD_SIMILARITY,
            AnalysisGoal.CLASS_SIMILARITY,
            AnalysisGoal.STRUCTURAL_CLONE,
        }
    ),
    "slop": frozenset(
        {
            AnalysisGoal.STRUCTURAL_CLONE,
            AnalysisGoal.SLOP,
            AnalysisGoal.STRUCTURE,
            AnalysisGoal.METHOD_SIMILARITY,
            AnalysisGoal.CLASS_SIMILARITY,
        }
    ),
    "smells": frozenset(
        {
            AnalysisGoal.STRUCTURE,
            AnalysisGoal.STATIC,
            AnalysisGoal.COVERAGE,
            AnalysisGoal.SLOP,
        }
    ),
    "strict": frozenset(g for g in AnalysisGoal if g != AnalysisGoal.CLUSTER),
}


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
    # Structural role inside its language ecosystem (e.g. react_component / react_hook)
    role: str = ""
    # Language-neutral text facets (filled by enrich/facets via DocPack)
    documentation: str | None = None
    signature: str | None = None
    body: str | None = None
    embedding: list[float] | None = None  # primary view = full
    embeddings: dict[str, list[float]] = Field(default_factory=dict)  # code, doc, …

    def describe_text(self) -> str:
        return self.description or self.source_snippet[:500]


class Finding(BaseModel):
    """Evidence produced by a sensor. ``action_hint`` is not policy."""

    model_config = ConfigDict(extra="ignore")

    id: str
    goal: AnalysisGoal = AnalysisGoal.STRUCTURE
    sensor: str = ""
    severity: Severity
    title: str
    evidence: str
    unit_ids: list[str] = Field(default_factory=list)
    action_hint: SuggestedAction = SuggestedAction.DOCUMENT
    score: float = 0.0
    metrics: dict[str, float] = Field(default_factory=dict)
    hints: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def suggested_action(self) -> SuggestedAction:
        return self.action_hint


class FindingValidationError(ValueError):
    """Raised when a finding cannot be constructed with resolvable targets."""


def make_finding(
    *,
    id: str,
    goal: AnalysisGoal,
    sensor: str,
    severity: Severity,
    title: str,
    evidence: str,
    unit_ids: list[str],
    action_hint: SuggestedAction = SuggestedAction.DOCUMENT,
    score: float = 0.0,
    metrics: dict[str, float] | None = None,
    hints: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
    known_ids: set[str] | None = None,
) -> Finding:
    unique = list(dict.fromkeys(uid for uid in unit_ids if uid))
    if not unique:
        raise FindingValidationError(f"finding {id!r} has empty unit_ids")
    if known_ids is not None:
        unresolved = [uid for uid in unique if uid not in known_ids]
        if unresolved:
            raise FindingValidationError(
                f"finding {id!r} has unresolved unit_ids: {unresolved[:5]}"
            )
    return Finding(
        id=id,
        goal=goal,
        sensor=sensor,
        severity=severity,
        title=title,
        evidence=evidence,
        unit_ids=unique,
        action_hint=action_hint,
        score=score,
        metrics=dict(metrics or {}),
        hints=list(hints or []),
        metadata=dict(metadata or {}),
    )


def try_make_finding(**kwargs: Any) -> Finding | None:
    try:
        return make_finding(**kwargs)
    except FindingValidationError:
        return None
    except Exception:  # noqa: BLE001 — pydantic ValidationError etc.
        return None


class PlannerDecision(BaseModel):
    title: str
    action: SuggestedAction = SuggestedAction.DOCUMENT
    rationale: str = ""
    confidence: float = 0.5
    finding_ids: list[str] = Field(default_factory=list)
    unit_ids: list[str] = Field(default_factory=list)
    disposition: PlanDisposition = "trash"
    valid: bool = False
    estimated_loc_delta: int = 0
    goal: AnalysisGoal | None = None

    @model_validator(mode="before")
    @classmethod
    def _normalize_disposition(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        data = dict(data)
        raw = str(data.get("disposition") or "").strip().lower()
        if raw in _PLAN_DISPOSITIONS:
            data["disposition"] = raw
            data["valid"] = raw != "trash"
            return data
        # Legacy: explicit valid without disposition.
        if "disposition" not in data and "valid" in data:
            keep = bool(data.get("valid"))
            data["disposition"] = "keep" if keep else "trash"
            data["valid"] = keep
            return data
        # Safe default: trash (do not rubber-stamp keep).
        data["disposition"] = "trash"
        data["valid"] = False
        return data

    @model_validator(mode="after")
    def _sync_valid_from_disposition(self) -> PlannerDecision:
        expected = self.disposition != "trash"
        if self.valid != expected:
            return self.model_copy(update={"valid": expected})
        return self


class UnitAgendaEntry(BaseModel):
    unit_id: str
    finding_ids: list[str] = Field(default_factory=list)
    goals: list[AnalysisGoal] = Field(default_factory=list)


class UnitAgenda(BaseModel):
    entries: list[UnitAgendaEntry] = Field(default_factory=list)

    def by_unit_id(self) -> dict[str, UnitAgendaEntry]:
        return {e.unit_id: e for e in self.entries}


class UnitCard(BaseModel):
    unit_id: str
    kind: str
    repo: str
    path: str
    qualified_name: str
    description: str = ""
    source_snippet: str = ""
    sibling_methods: list[str] = Field(default_factory=list)


class PlanningBrief(BaseModel):
    """Agenda-first input to the conflict planner."""

    component_id: str
    units: list[UnitCard] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    findings_by_goal: dict[str, list[str]] = Field(default_factory=dict)
    hints: list[str] = Field(default_factory=list)
    candidate_actions: list[SuggestedAction] = Field(default_factory=list)


class ConfirmedFinding(BaseModel):
    finding: Finding
    valid: bool
    reasoning: str
    refined_action: SuggestedAction | None = None
    disposition: PlanDisposition | None = None

    @model_validator(mode="before")
    @classmethod
    def _accept_flag_alias(cls, data: Any) -> Any:
        if isinstance(data, dict) and "flag" in data and "finding" not in data:
            data = {**data, "finding": data["flag"]}
        return data

    @property
    def flag(self) -> Finding:
        """0.1 compat alias."""
        return self.finding

class SensorReport(BaseModel):
    sensor: str = ""
    status: str = "ran"  # ran | skipped | error
    detail: str = ""
    finding_count: int = 0

    @model_validator(mode="before")
    @classmethod
    def _accept_legacy_fields(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data = dict(data)
            if "name" in data and not data.get("sensor"):
                data["sensor"] = data.pop("name")
            if "flag_count" in data and "finding_count" not in data:
                data["finding_count"] = data.pop("flag_count")
        return data

    @property
    def name(self) -> str:
        return self.sensor

    @property
    def flag_count(self) -> int:
        return self.finding_count


class RunManifest(BaseModel):
    schema_version: int = SCHEMA_VERSION
    created_at: str
    workspace: str
    repos_scanned: list[str]
    unit_count: int
    finding_count: int = 0
    confirmed_count: int = 0
    llm_model: str
    embed_model: str
    similarity_threshold: float
    out_dir: str = ""
    focus: str = "balanced"
    sensors_ran: list[str] = Field(default_factory=list)
    chat_backend: str = "http"
    embed_backend: str = "http"
    describe_mode: str = "raw"

    @property
    def flag_count(self) -> int:
        return self.finding_count


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
    schema_version: int = SCHEMA_VERSION
    summary: str = ""
    overall_percent: float | None = None
    files: list[CoverageFileStat] = Field(default_factory=list)
    source: str = "unknown"  # missing | jacoco | coverage.py | istanbul | merged | …
    repos_without_coverage: list[str] = Field(default_factory=list)


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


class AnalysisArtifacts(BaseModel):
    units: list[CodeUnit] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    confirmed: list[ConfirmedFinding] = Field(default_factory=list)
    agenda: UnitAgenda | None = None
    plan: RefactorPlan | None = None
    test_plan: TestPlan | None = None
    test_harness: TestHarnessResult | None = None
    manifest: RunManifest | None = None
    document_report: DocumentReport | None = None
    coverage: CoverageSnapshot | None = None
    sensor_reports: list[SensorReport] = Field(default_factory=list)

    @property
    def flags(self) -> list[Finding]:
        return self.findings

    @property
    def analyzer_notes(self) -> list[SensorReport]:
        return self.sensor_reports


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def unit_id(*parts: str) -> str:
    import hashlib

    return hashlib.sha256(":".join(parts).encode("utf-8")).hexdigest()


def content_hash(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()
