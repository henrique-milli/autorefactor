from __future__ import annotations

import json
from pathlib import Path

from autorefactor.models import (
    AnalysisArtifacts,
    CodeUnit,
    ConfirmedFinding,
    RefactorPlan,
    RunManifest,
    TestHarnessResult,
    TestPlan,
)


def write_manifest(path: Path, manifest: RunManifest) -> None:
    path.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")


def write_findings_json(path: Path, findings: list[ConfirmedFinding]) -> None:
    payload = [f.model_dump(mode="json") for f in findings]
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def write_units_json(path: Path, units: list[CodeUnit]) -> None:
    slim = []
    for unit in units:
        data = unit.model_dump(mode="json")
        if data.get("embedding"):
            data["embedding_dims"] = len(unit.embedding or [])
            del data["embedding"]
        emb_map = data.get("embeddings") or {}
        if emb_map:
            data["embeddings_dims"] = {k: len(v) for k, v in emb_map.items() if v}
            del data["embeddings"]
        slim.append(data)
    path.write_text(json.dumps(slim, indent=2), encoding="utf-8")


def render_plan_markdown(
    plan: RefactorPlan,
    findings: list[ConfirmedFinding],
    units: list[CodeUnit],
) -> str:
    by_id = {u.id: u for u in units}
    lines = [
        "# autorefactor.ai refactor plan",
        "",
        plan.summary,
        "",
        f"- Confirmed findings: **{plan.confirmed_count}**",
        f"- Rejected findings: **{plan.rejected_count}**",
        f"- Steps: **{len(plan.steps)}**",
        f"- Tests required before refactor: **{plan.tests_required}**",
        "",
        "> **Gate:** Run `autorefactor test` and get a green harness before `autorefactor refactor`.",
        "",
        "## Steps",
        "",
    ]
    for step in plan.steps:
        lines.extend(
            [
                f"### {step.step_id} — {step.title}",
                "",
                f"- Action: `{step.action.value}`",
                f"- Blast radius: {step.blast_radius}",
                f"- Acceptance: {step.acceptance}",
                f"- Rationale: {step.rationale}",
                f"- Targets ({len(step.target_paths)}):",
            ]
        )
        for path in step.target_paths[:20]:
            lines.append(f"  - `{path}`")
        lines.append("")

    lines.extend(["## Confirmed findings", ""])
    for finding in findings:
        if not finding.valid:
            continue
        action = (finding.refined_action or finding.flag.suggested_action).value
        lines.append(f"- **{finding.flag.title}** (`{action}`)")
        lines.append(f"  - {finding.reasoning}")
        for uid in finding.flag.unit_ids[:4]:
            unit = by_id.get(uid)
            if unit:
                lines.append(f"  - `{unit.repo}/{unit.path}` :: `{unit.qualified_name}`")
        lines.append("")

    rejected = [f for f in findings if not f.valid]
    if rejected:
        lines.extend(["## Rejected flags", ""])
        for finding in rejected[:50]:
            lines.append(f"- ~~{finding.flag.title}~~ — {finding.reasoning}")
        lines.append("")

    return "\n".join(lines)


def write_plan_markdown(
    path: Path,
    plan: RefactorPlan,
    findings: list[ConfirmedFinding],
    units: list[CodeUnit],
) -> None:
    path.write_text(render_plan_markdown(plan, findings, units), encoding="utf-8")


def render_test_plan_markdown(test_plan: TestPlan) -> str:
    lines = [
        "# autorefactor.ai test plan (pre-refactor)",
        "",
        test_plan.summary,
        "",
        f"- Recommended command: `{test_plan.recommended_cmd or 'pytest -q'}`",
        f"- Existing test files: **{len(test_plan.existing_test_files)}**",
        f"- Gaps to cover: **{len(test_plan.gaps)}**",
        "",
        "## Existing tests",
        "",
    ]
    if test_plan.existing_test_files:
        for path in test_plan.existing_test_files[:50]:
            lines.append(f"- `{path}`")
    else:
        lines.append("- (none detected)")
    lines.extend(["", "## Characterization gaps", ""])
    if not test_plan.gaps:
        lines.append("- No gaps — existing coverage looks sufficient for confirmed targets.")
    for gap in test_plan.gaps:
        lines.extend(
            [
                f"- `{gap.repo}/{gap.path}` :: `{gap.qualified_name}` ({gap.language})",
                f"  - reason: {gap.reason}",
                f"  - write: `{gap.suggested_test_path}`",
            ]
        )
    lines.append("")
    return "\n".join(lines)


def write_test_plan_markdown(path: Path, test_plan: TestPlan) -> None:
    path.write_text(render_test_plan_markdown(test_plan), encoding="utf-8")


def write_test_report(path: Path, result: TestHarnessResult) -> None:
    body = (
        "# autorefactor test harness report\n\n"
        f"- passed: **{result.passed}**\n"
        f"- skipped: **{result.skipped}**\n\n"
        f"{result.summary}\n\n"
        f"## Created files ({len(result.created_files)})\n"
        + ("\n".join(f"- `{p}`" for p in result.created_files) or "- (none)")
        + "\n\n## Test output\n\n```\n"
        + (result.test_output[:8000] or "(no output)")
        + "\n```\n"
    )
    path.write_text(body, encoding="utf-8")


def write_artifacts_bundle(out_dir: Path, artifacts: AnalysisArtifacts) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    if artifacts.manifest:
        write_manifest(out_dir / "manifest.json", artifacts.manifest)
    write_findings_json(out_dir / "findings.json", artifacts.findings)
    write_units_json(out_dir / "units.json", artifacts.units)
    if artifacts.plan:
        write_plan_markdown(
            out_dir / "plan.md",
            artifacts.plan,
            artifacts.findings,
            artifacts.units,
        )
        (out_dir / "plan.json").write_text(
            artifacts.plan.model_dump_json(indent=2),
            encoding="utf-8",
        )
    if artifacts.test_plan:
        write_test_plan_markdown(out_dir / "test-plan.md", artifacts.test_plan)
        (out_dir / "test-plan.json").write_text(
            artifacts.test_plan.model_dump_json(indent=2),
            encoding="utf-8",
        )
    if artifacts.test_harness:
        write_test_report(out_dir / "test-report.md", artifacts.test_harness)
        (out_dir / "test-harness.json").write_text(
            artifacts.test_harness.model_dump_json(indent=2),
            encoding="utf-8",
        )
