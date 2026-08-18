from __future__ import annotations

import json
from pathlib import Path

from autorefactor.artifacts.paths import ArtifactLayout, layout_for
from autorefactor.index.store import IndexStore, write_jsonl_units
from autorefactor.models import (
    AnalysisArtifacts,
    AnalyzerRunNote,
    CodeUnit,
    ConfirmedFinding,
    CoverageSnapshot,
    DocumentReport,
    Finding,
    Flag,
    RefactorPlan,
    RunManifest,
    TestHarnessResult,
    TestPlan,
)
from autorefactor.report import (
    render_plan_markdown,
    render_test_plan_markdown,
    write_findings_json,
    write_manifest,
    write_test_report,
    write_units_json,
)


class MissingArtifactsError(RuntimeError):
    """Raised when a stage cannot find required upstream outputs."""


def _read_json(path: Path) -> object | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def open_store(layout: ArtifactLayout) -> IndexStore:
    return IndexStore(layout.db_path)


def save_index_units(layout: ArtifactLayout, units: list[CodeUnit]) -> None:
    write_jsonl_units(layout.index_dir / "units.jsonl", units)
    write_units_json(layout.index_dir / "units.json", units)
    # Compatibility mirror at out_dir root for older consumers
    write_units_json(layout.out_dir / "units.json", units)
    write_jsonl_units(layout.out_dir / "units.jsonl", units)


def save_analyze(
    layout: ArtifactLayout,
    *,
    units: list[CodeUnit],
    findings: list | None = None,
    confirmed: list[ConfirmedFinding] | None = None,
    manifest: RunManifest,
    sensor_reports: list | None = None,
    flags: list | None = None,
    analyzer_notes: list | None = None,
) -> None:
    evidence = findings if findings is not None else (flags or [])
    confirmed_list = confirmed or []
    notes = sensor_reports if sensor_reports is not None else (analyzer_notes or [])
    analyze = layout.analyze_dir
    _write_json(analyze / "findings.json", [f.model_dump(mode="json") for f in evidence])
    _write_json(analyze / "flags.json", [f.model_dump(mode="json") for f in evidence])  # compat
    write_findings_json(analyze / "confirmed.json", confirmed_list)
    write_findings_json(analyze / "findings_confirmed.json", confirmed_list)
    write_manifest(analyze / "manifest.json", manifest)
    write_units_json(analyze / "units.json", units)
    _write_json(analyze / "sensor-reports.json", [n.model_dump(mode="json") for n in notes])
    report_lines = [
        "# autorefactor analyze report",
        "",
        f"- Units: **{len(units)}**",
        f"- Findings: **{len(evidence)}**",
        f"- Confirmed: **{sum(1 for f in confirmed_list if f.valid)}**",
        f"- Rejected: **{sum(1 for f in confirmed_list if not f.valid)}**",
        "",
        "## Sensors",
        "",
    ]
    for note in notes:
        name = getattr(note, "sensor", None) or getattr(note, "name", "?")
        count = getattr(note, "finding_count", None)
        if count is None:
            count = getattr(note, "flag_count", 0)
        report_lines.append(
            f"- `{name}`: {note.status} ({count} findings) {note.detail}".rstrip()
        )
    report_lines.extend(["", "## Top confirmed findings", ""])
    for item in [f for f in confirmed_list if f.valid][:30]:
        ev = item.finding
        action = (item.refined_action or ev.action_hint).value
        report_lines.append(f"- **{ev.title}** (`{action}`) — {item.reasoning}")
    (analyze / "report.md").write_text("\n".join(report_lines) + "\n", encoding="utf-8")

    write_findings_json(layout.out_dir / "findings.json", confirmed_list)
    write_manifest(layout.out_dir / "manifest.json", manifest)


def save_document(layout: ArtifactLayout, report: DocumentReport) -> None:
    path = layout.document_dir
    _write_json(path / "document-report.json", report.model_dump(mode="json"))
    lines = [
        "# autorefactor document report",
        "",
        report.summary,
        "",
        f"- Gaps found: **{len(report.gaps)}**",
        f"- Touched paths: **{len(report.touched_paths)}**",
        f"- Skipped: **{report.skipped}**",
        "",
        "## Gaps",
        "",
    ]
    for gap in report.gaps[:100]:
        lines.append(
            f"- `{gap.repo}/{gap.path}` :: `{gap.qualified_name}` ({gap.language}) — {gap.reason}"
        )
    lines.extend(["", "## Touched paths", ""])
    for touched in report.touched_paths[:100]:
        lines.append(f"- `{touched}`")
    if not report.touched_paths:
        lines.append("- (none)")
    (path / "document-report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def save_test(
    layout: ArtifactLayout,
    *,
    test_plan: TestPlan | None = None,
    harness: TestHarnessResult | None = None,
    coverage: CoverageSnapshot | None = None,
) -> None:
    test_dir = layout.test_dir
    if test_plan is not None:
        (test_dir / "test-plan.json").write_text(
            test_plan.model_dump_json(indent=2), encoding="utf-8"
        )
        (test_dir / "test-plan.md").write_text(
            render_test_plan_markdown(test_plan), encoding="utf-8"
        )
        # compatibility
        (layout.out_dir / "test-plan.json").write_text(
            test_plan.model_dump_json(indent=2), encoding="utf-8"
        )
        (layout.out_dir / "test-plan.md").write_text(
            render_test_plan_markdown(test_plan), encoding="utf-8"
        )
    if coverage is not None:
        _write_json(test_dir / "coverage.json", coverage.model_dump(mode="json"))
    if harness is not None:
        write_test_report(test_dir / "test-report.md", harness)
        (test_dir / "test-harness.json").write_text(
            harness.model_dump_json(indent=2), encoding="utf-8"
        )
        write_test_report(layout.out_dir / "test-report.md", harness)
        (layout.out_dir / "test-harness.json").write_text(
            harness.model_dump_json(indent=2), encoding="utf-8"
        )


def save_plan(
    layout: ArtifactLayout,
    plan: RefactorPlan,
    findings: list[ConfirmedFinding],
    units: list[CodeUnit],
) -> None:
    plan_dir = layout.plan_dir
    (plan_dir / "plan.json").write_text(plan.model_dump_json(indent=2), encoding="utf-8")
    (plan_dir / "plan.md").write_text(
        render_plan_markdown(plan, findings, units), encoding="utf-8"
    )
    (layout.out_dir / "plan.json").write_text(plan.model_dump_json(indent=2), encoding="utf-8")
    (layout.out_dir / "plan.md").write_text(
        render_plan_markdown(plan, findings, units), encoding="utf-8"
    )


def load_units(layout: ArtifactLayout) -> list[CodeUnit]:
    # Prefer analyze units (may include embeddings/descriptions), then index
    for candidate in (
        layout.analyze_dir / "units.json",
        layout.index_dir / "units.json",
        layout.out_dir / "units.json",
    ):
        raw = _read_json(candidate)
        if isinstance(raw, list) and raw:
            return [CodeUnit.model_validate(u) for u in raw]
    store = open_store(layout)
    try:
        artifacts = store.load_artifacts()
    finally:
        store.close()
    if artifacts and artifacts.units:
        return artifacts.units
    raise MissingArtifactsError(
        f"No indexed units found under {layout.out_dir}. Run `autorefactor index` or `analyze` first."
    )


def load_analyze_bundle(
    layout: ArtifactLayout,
) -> tuple[list[CodeUnit], list[Finding], list[ConfirmedFinding], RunManifest | None, list]:
    units = load_units(layout)
    flags_raw = _read_json(layout.analyze_dir / "flags.json") or _read_json(
        layout.analyze_dir / "findings.json"
    )
    confirmed_raw = (
        _read_json(layout.analyze_dir / "confirmed.json")
        or _read_json(layout.analyze_dir / "findings_confirmed.json")
        or _read_json(layout.out_dir / "findings.json")
    )
    manifest_raw = _read_json(layout.analyze_dir / "manifest.json") or _read_json(
        layout.out_dir / "manifest.json"
    )
    notes_raw = (
        _read_json(layout.analyze_dir / "sensor-reports.json")
        or _read_json(layout.analyze_dir / "analyzer-notes.json")
    )
    if flags_raw is None and confirmed_raw is None:
        store = open_store(layout)
        try:
            legacy = store.load_artifacts()
        finally:
            store.close()
        if legacy and (legacy.findings or legacy.confirmed):
            return (
                legacy.units or units,
                legacy.findings,
                legacy.confirmed,
                legacy.manifest,
                legacy.sensor_reports,
            )
        raise MissingArtifactsError(
            f"No analyze findings under {layout.analyze_dir}. Run `autorefactor analyze` first."
        )
    evidence = [Finding.model_validate(f) for f in (flags_raw or [])]
    confirmed: list[ConfirmedFinding] = []
    for item in confirmed_raw or []:
        if isinstance(item, dict) and ("finding" in item or "flag" in item):
            confirmed.append(ConfirmedFinding.model_validate(item))
    manifest = RunManifest.model_validate(manifest_raw) if manifest_raw else None
    from autorefactor.domain import SensorReport

    notes = [SensorReport.model_validate(n) for n in (notes_raw or [])]
    return units, evidence, confirmed, manifest, notes


def load_test_harness(layout: ArtifactLayout) -> TestHarnessResult | None:
    raw = _read_json(layout.test_dir / "test-harness.json") or _read_json(
        layout.out_dir / "test-harness.json"
    )
    if raw is None:
        return None
    return TestHarnessResult.model_validate(raw)


def load_test_plan(layout: ArtifactLayout) -> TestPlan | None:
    raw = _read_json(layout.test_dir / "test-plan.json") or _read_json(
        layout.out_dir / "test-plan.json"
    )
    if raw is None:
        return None
    return TestPlan.model_validate(raw)


def load_coverage(layout: ArtifactLayout) -> CoverageSnapshot | None:
    raw = (
        _read_json(layout.out_dir / "index" / "coverage.json")
        or _read_json(layout.out_dir / "enrich" / "coverage.json")
        or _read_json(layout.test_dir / "coverage.json")
    )
    if raw is None:
        return None
    return CoverageSnapshot.model_validate(raw)


def load_document_report(layout: ArtifactLayout) -> DocumentReport | None:
    raw = _read_json(layout.document_dir / "document-report.json")
    if raw is None:
        return None
    return DocumentReport.model_validate(raw)


def load_plan(layout: ArtifactLayout) -> RefactorPlan | None:
    raw = _read_json(layout.plan_dir / "plan.json") or _read_json(layout.out_dir / "plan.json")
    if raw is None:
        return None
    return RefactorPlan.model_validate(raw)


def load_artifacts(out_dir: Path) -> AnalysisArtifacts:
    """Assemble a full AnalysisArtifacts view from stage dirs (+ legacy sqlite)."""
    layout = layout_for(out_dir)
    store = open_store(layout)
    legacy = store.load_artifacts()
    store.close()

    try:
        units, flags, findings, manifest, notes = load_analyze_bundle(layout)
    except MissingArtifactsError:
        if legacy:
            units, flags, findings, manifest, notes = (
                legacy.units,
                legacy.flags,
                legacy.findings,
                legacy.manifest,
                legacy.analyzer_notes,
            )
        else:
            raise

    plan = load_plan(layout) or (legacy.plan if legacy else None)
    test_plan = load_test_plan(layout) or (legacy.test_plan if legacy else None)
    harness = load_test_harness(layout) or (legacy.test_harness if legacy else None)
    document_report = load_document_report(layout) or (
        legacy.document_report if legacy else None
    )
    coverage = load_coverage(layout) or (legacy.coverage if legacy else None)

    return AnalysisArtifacts(
        units=units,
        flags=flags,
        findings=findings,
        plan=plan,
        test_plan=test_plan,
        test_harness=harness,
        manifest=manifest,
        document_report=document_report,
        coverage=coverage,
        analyzer_notes=notes,
    )


def persist_sqlite_snapshot(layout: ArtifactLayout, artifacts: AnalysisArtifacts) -> None:
    store = open_store(layout)
    store.save_artifacts(artifacts)
    store.close()
