from __future__ import annotations

from rich.console import Console

from coderazor.artifacts import layout_for, open_store, persist_sqlite_snapshot, save_index_units
from coderazor.config import CoderazorConfig
from coderazor.extract import extract_workspace
from coderazor.index.hierarchy import ensure_repo_units, filter_units
from coderazor.models import AnalysisArtifacts, CodeUnit

console = Console()


def run_index(config: CoderazorConfig) -> list[CodeUnit]:
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
        flags=existing.flags if existing else [],
        findings=existing.findings if existing else [],
        plan=existing.plan if existing else None,
        test_plan=existing.test_plan if existing else None,
        test_harness=existing.test_harness if existing else None,
        manifest=existing.manifest if existing else None,
        document_report=existing.document_report if existing else None,
        coverage=existing.coverage if existing else None,
        analyzer_notes=existing.analyzer_notes if existing else [],
    )
    store.save_artifacts(artifacts)
    store.close()
    console.print(f"[green]Indexed {len(units)} units → {layout.index_dir}[/green]")
    return units
