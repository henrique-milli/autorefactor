from __future__ import annotations

import json
import re
from pathlib import Path

from autorefactor.domain import (
    AnalysisGoal,
    CodeUnit,
    CoverageFileStat,
    CoverageSnapshot,
    Finding,
    Severity,
    SuggestedAction,
    UnitKind,
    content_hash,
    try_make_finding,
)
from autorefactor.enrich.unit_match import units_covering_line
from autorefactor.sensors.anchors import production_type_for_repo
from autorefactor.sensors.base import AnalysisContext

_APPLICATION_PATH = re.compile(r"Application\.(java|kt)$", re.IGNORECASE)
_EXCEPTION_PATH = re.compile(r"Exception\.(java|kt)$", re.IGNORECASE)
_GENERATED_IMPL = re.compile(r"MapperImpl\.(java|kt)$", re.IGNORECASE)
_NOISE_BASENAME = re.compile(
    r"(OpenApiConfig|SwaggerConfig|AsyncConfig|JacksonConfig|WebMvcConfig|"
    r"CorsConfig|SecurityConfig|SnowflakeFilter|.*ControllerAdvice|"
    r".*ExceptionHandler)\.(java|kt)$",
    re.IGNORECASE,
)
# Prefer substantive business surface area over bean wiring / filters
_MIN_EXECUTABLE_LINES = 15


def _uncovered_lines(stat: CoverageFileStat) -> float:
    if stat.total_lines <= 0:
        return 0.0
    covered = max(0.0, min(float(stat.covered_lines), float(stat.total_lines)))
    return max(0.0, float(stat.total_lines) - covered)


def _coverage_path_skipped(stat: CoverageFileStat) -> bool:
    """Drop bootstrap / generated / tiny / low-ROI coverage noise."""
    path = stat.path.replace("\\", "/")
    base = path.rsplit("/", 1)[-1]
    lower = path.lower()

    if _APPLICATION_PATH.search(base):
        return True
    if _GENERATED_IMPL.search(base):
        return True
    if _NOISE_BASENAME.search(base):
        return True
    if stat.total_lines > 0 and stat.total_lines < _MIN_EXECUTABLE_LINES:
        return True
    if _EXCEPTION_PATH.search(base) and stat.percent >= 40.0:
        return True
    # DTO / model packages — rarely worth ensure_tests in the queue
    if "/dto/" in lower or lower.startswith("dto/") or "/model/" in lower or "/models/" in lower:
        return True
    # Small Spring config / filter packages with little executable surface
    if ("/config/" in lower or "/filter/" in lower or "/filters/" in lower) and stat.total_lines < 40:
        return True
    return False


def _primary_coverage_unit(matched: list[CodeUnit], fallback: list[CodeUnit]) -> CodeUnit | None:
    pool = matched or fallback
    if not pool:
        return None
    for kind in (UnitKind.FILE, UnitKind.TYPE, UnitKind.MODULE, UnitKind.FUNCTION):
        for u in pool:
            if u.kind == kind:
                return u
    return pool[0]


def coverage_tool_configured(repo: Path) -> bool:
    """True when the build already declares a coverage tool (report may still be absent)."""
    repo = repo.resolve()
    checked = 0
    max_files = 40

    def _text_has_java_coverage(text: str) -> bool:
        return "jacoco" in text

    def _text_has_js_coverage(text: str) -> bool:
        return any(
            token in text
            for token in ("istanbul", '"nyc"', '"c8"', "'nyc'", "'c8'", "vitest")
        )

    # Nested Maven / Gradle modules (capped)
    for pattern in ("pom.xml", "build.gradle", "build.gradle.kts"):
        for path in repo.rglob(pattern):
            if checked >= max_files:
                break
            # Skip huge generated trees
            parts = set(path.parts)
            if parts & {"node_modules", "target", "build", ".git"}:
                continue
            checked += 1
            try:
                text = path.read_text(encoding="utf-8", errors="ignore").lower()
            except OSError:
                continue
            if path.name == "pom.xml" or path.name.startswith("build.gradle"):
                if _text_has_java_coverage(text):
                    return True
        if checked >= max_files:
            break

    pkg = repo / "package.json"
    if pkg.exists():
        text = pkg.read_text(encoding="utf-8", errors="ignore").lower()
        if _text_has_js_coverage(text):
            return True
    if (repo / ".coveragerc").exists():
        return True
    pyproject = repo / "pyproject.toml"
    if pyproject.exists():
        text = pyproject.read_text(encoding="utf-8", errors="ignore").lower()
        if "[tool.coverage" in text or "pytest-cov" in text:
            return True
    return False


def _mark_estimated_when_tooling_only(
    snap: CoverageSnapshot,
    repos: list[Path],
) -> CoverageSnapshot:
    """Jacoco/Istanbul configured but no indexed report → estimated-only snapshot."""
    if snap.files:
        return snap
    source = (snap.source or "").lower()
    if source not in {"missing", "unknown", ""} and source != "estimated":
        # Real parser source with empty files — still treat as unindexed
        pass
    configured = [p for p in repos if coverage_tool_configured(p)]
    if not configured:
        return snap
    missing = sorted({p.name for p in repos})
    return snap.model_copy(
        update={
            "source": "estimated",
            "files": [],
            "repos_without_coverage": missing,
            "summary": (
                snap.summary
                or "Coverage tooling detected but no Jacoco/XML artifacts indexed"
            ),
        }
    )


def enrich_coverage(
    *,
    workspace: Path,
    repos: list[Path],
    out_path: Path | None = None,
) -> CoverageSnapshot:
    from autorefactor.enrich.coverage_parsers import load_coverage_snapshot

    try:
        snap = load_coverage_snapshot(workspace, repos)
    except Exception:  # noqa: BLE001 — never fail analyze on coverage import
        snap = None
    generate_notes: list[str] = []
    if snap is None or not snap.files:
        from autorefactor.enrich.coverage_generate import try_generate_coverage_reports

        gen = try_generate_coverage_reports(workspace, repos)
        generate_notes = list(gen.get("notes") or [])
        if int(gen.get("ran") or 0) > 0:
            try:
                snap = load_coverage_snapshot(workspace, repos)
            except Exception:  # noqa: BLE001
                snap = None
    if snap is None:
        snap = CoverageSnapshot(
            summary="No coverage report found in workspace",
            overall_percent=None,
            files=[],
            source="missing",
            repos_without_coverage=[p.name for p in repos],
        )
    snap = _mark_estimated_when_tooling_only(snap, repos)
    if generate_notes:
        extra = "Coverage generate: " + "; ".join(generate_notes[:4])
        snap = snap.model_copy(
            update={"summary": ((snap.summary or "").strip() + " | " + extra).strip(" |")}
        )
    if not snap.repos_without_coverage:
        # Ensure partial multi-repo gaps are listed even if parsers left it empty
        reported = set()
        for path in snap.files:
            parts = path.path.replace("\\", "/").split("/", 1)
            if parts and any(p.name == parts[0] for p in repos):
                reported.add(parts[0])
        # Prefer explicit field from merge; leave as-is if already set
        pass
    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        # Compat: also mirror under enrich/ when writing to index/
        payload = snap.model_dump(mode="json")
        out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        if out_path.parent.name == "index":
            legacy = out_path.parent.parent / "enrich" / "coverage.json"
            legacy.parent.mkdir(parents=True, exist_ok=True)
            legacy.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return snap


class CoverageSensor:
    id = "coverage"
    goal = AnalysisGoal.COVERAGE

    def available(self, ctx: AnalysisContext) -> bool:
        return True

    def collect(self, ctx: AnalysisContext) -> list[Finding]:
        known = ctx.known_ids()
        snap = ctx.coverage
        if snap is None:
            from autorefactor.enrich.coverage_parsers import load_coverage_snapshot

            snap = load_coverage_snapshot(ctx.workspace, list(ctx.repos))
        findings: list[Finding] = []

        missing_repos: list[str] = []
        source = (snap.source or "").lower() if snap else "missing"
        if snap is None or source in {"missing", "estimated"} or not snap.files:
            missing_repos = [p.name for p in ctx.repos]
        else:
            missing_repos = list(snap.repos_without_coverage or [])

        if missing_repos:
            repo_units = {u.repo: u for u in ctx.units if u.kind == UnitKind.REPO}
            repo_paths = {p.name: p for p in ctx.repos}
            for repo in missing_repos:
                repo_path = repo_paths.get(repo)
                if repo_path is not None and coverage_tool_configured(repo_path):
                    # Jacoco/Istanbul declared but XML not indexed — still queue ensure_tests.
                    prod = production_type_for_repo(ctx, repo)
                    if prod is None:
                        continue
                    f = try_make_finding(
                        id=content_hash(f"coverage:estimated:{repo}"),
                        goal=self.goal,
                        sensor=self.id,
                        severity=Severity.HIGH,
                        title=f"Coverage tooling present but no indexed report for {repo}",
                        evidence=(
                            "JaCoCo/Istanbul is configured in the build but no coverage XML "
                            "was indexed (eval copies omit target/). Queue characterization "
                            "tests on production types before structural refactors."
                        ),
                        unit_ids=[prod.id],
                        action_hint=SuggestedAction.ENSURE_TESTS,
                        score=0.84,
                        metrics={},
                        metadata={
                            "kind": "coverage_estimated",
                            "repo": repo,
                            "path": prod.path,
                        },
                        known_ids=known,
                    )
                    if f:
                        findings.append(f)
                    continue
                unit = repo_units.get(repo)
                if unit is None:
                    members = [u for u in ctx.units if u.repo == repo]
                    if not members:
                        continue
                    unit = members[0]
                f = try_make_finding(
                    id=content_hash(f"coverage:missing:{repo}"),
                    goal=self.goal,
                    sensor=self.id,
                    severity=Severity.HIGH,
                    title=f"No coverage report for {repo}",
                    evidence=(
                        "No coverage report found and no coverage plugin detected in the "
                        "build files. Add JaCoCo/Istanbul/coverage.py instrumentation, or "
                        "mount a generated report before structural refactors."
                    ),
                    unit_ids=[unit.id],
                    action_hint=SuggestedAction.ENSURE_TESTS,
                    score=0.85,
                    metrics={},
                    metadata={"missing_report": True, "repo": repo},
                    known_ids=known,
                )
                if f:
                    findings.append(f)

        if snap is None or source in {"missing", "estimated"} or not snap.files:
            return findings

        threshold = ctx.config.coverage_threshold
        max_per_repo = max(1, int(getattr(ctx.config, "max_coverage_findings", 15) or 15))
        # Collect candidates then keep top-N per repo by uncovered lines
        candidates: list[tuple[float, CoverageFileStat, CodeUnit]] = []
        for stat in snap.files:
            if stat.total_lines <= 0 or stat.percent >= threshold:
                continue
            if _coverage_path_skipped(stat):
                continue
            matched = units_covering_line(list(ctx.units), stat.path, 1)
            fallback = [
                u
                for u in ctx.units
                if u.path.replace("\\", "/").endswith(stat.path.split("/")[-1])
            ]
            primary = _primary_coverage_unit(matched, fallback)
            if primary is None:
                continue
            candidates.append((_uncovered_lines(stat), stat, primary))

        by_repo: dict[str, list[tuple[float, CoverageFileStat, CodeUnit]]] = {}
        for item in candidates:
            by_repo.setdefault(item[2].repo, []).append(item)

        for repo, items in by_repo.items():
            items.sort(key=lambda t: (-t[0], t[1].percent, t[1].path))
            kept = items[:max_per_repo]
            max_unc = max((u for u, _, _ in kept), default=1.0) or 1.0
            for uncovered, stat, primary in kept:
                # Rank by how much executable code is untested, not flat 0% ties
                gap = (threshold - stat.percent) / max(threshold, 1.0)
                volume = uncovered / max_unc
                score = max(0.3, min(1.0, 0.55 * gap + 0.45 * volume))
                f = try_make_finding(
                    id=content_hash(f"coverage:{stat.path}:{stat.percent}"),
                    goal=self.goal,
                    sensor=self.id,
                    severity=Severity.HIGH if uncovered >= 40 or stat.percent < threshold / 2 else Severity.MEDIUM,
                    title=f"Low coverage {stat.path}",
                    evidence=(
                        f"{stat.percent:.1f}% lines covered "
                        f"(~{uncovered:.0f}/{stat.total_lines} uncovered; threshold {threshold}%)"
                    ),
                    unit_ids=[primary.id],
                    action_hint=SuggestedAction.ENSURE_TESTS,
                    score=score,
                    metrics={
                        "percent": float(stat.percent),
                        "uncovered_lines": float(uncovered),
                        "total_lines": float(stat.total_lines),
                    },
                    metadata={"path": stat.path, "source": snap.source, "repo": repo},
                    known_ids=known,
                )
                if f:
                    findings.append(f)
        return findings
