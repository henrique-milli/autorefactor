from __future__ import annotations

from rich.console import Console

from autorefactor.artifacts import layout_for, open_store, persist_sqlite_snapshot, save_index_units
from autorefactor.config import AutorefactorConfig
from autorefactor.extract import extract_workspace
from autorefactor.index.hierarchy import ensure_repo_units, filter_units
from autorefactor.models import AnalysisArtifacts, CodeUnit

console = Console()


def run_index(config: AutorefactorConfig) -> list[CodeUnit]:
    layout = layout_for(config.resolve_out_dir())
    repos = config.discover_repos()
    console.print(f"[bold]Indexing repos in {config.workspace}[/bold] ({len(repos)} found)")
    for repo in repos:
        console.print(f"  • {repo.name}")

    units = extract_workspace(repos)
    units = filter_units(units)
    units = ensure_repo_units(units)
    if not units:
        raise RuntimeError("No code units extracted from workspace.")

    save_index_units(layout, units)
    store = open_store(layout)
    # Preserve description cache; only refresh units snapshot in sqlite when present
    existing = store.load_artifacts()
    artifacts = AnalysisArtifacts(
        units=units,
        findings=existing.findings if existing else [],
        confirmed=existing.confirmed if existing else [],
        plan=existing.plan if existing else None,
        test_plan=existing.test_plan if existing else None,
        test_harness=existing.test_harness if existing else None,
        manifest=existing.manifest if existing else None,
        document_report=existing.document_report if existing else None,
        coverage=existing.coverage if existing else None,
        sensor_reports=existing.sensor_reports if existing else [],
    )
    store.save_artifacts(artifacts)
    store.close()
    console.print(f"[green]Indexed {len(units)} units → {layout.index_dir}[/green]")
    return units
