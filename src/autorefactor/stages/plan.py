from __future__ import annotations

from rich.console import Console

from autorefactor.artifacts import (
    MissingArtifactsError,
    layout_for,
    load_artifacts,
    load_test_harness,
    persist_sqlite_snapshot,
    save_plan,
)
from autorefactor.config import AutorefactorConfig
from autorefactor.models import AnalysisArtifacts
from autorefactor._legacy_plan import build_plan

console = Console()


def run_plan(config: AutorefactorConfig, *, require_harness: bool | None = None) -> AnalysisArtifacts:
    layout = layout_for(config.resolve_out_dir())
    artifacts = load_artifacts(layout.out_dir)
    if not artifacts.findings:
        raise MissingArtifactsError("No findings to plan from. Run `autorefactor analyze` first.")

    harness_required = config.require_harness if require_harness is None else require_harness
    harness = load_test_harness(layout) or artifacts.test_harness
    if harness_required and config.require_tests:
        if harness is None or not harness.passed:
            raise RuntimeError(
                "Test harness missing or failed. Run `autorefactor test` first "
                "(or pass --no-require-harness)."
            )

    skip_docs = bool(artifacts.document_report and not artifacts.document_report.gaps)
    plan = build_plan(
        artifacts.findings,
        artifacts.units,
        tests_required=config.require_tests,
        skip_document_actions=skip_docs,
    )
    artifacts.plan = plan
    artifacts.test_harness = harness
    save_plan(layout, plan, artifacts.findings, artifacts.units)
    persist_sqlite_snapshot(layout, artifacts)
    console.print(f"[green]Plan → {layout.plan_dir} ({len(plan.steps)} steps)[/green]")
    console.print(plan.summary)
    return artifacts
