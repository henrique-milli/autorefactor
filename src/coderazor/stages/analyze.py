from __future__ import annotations

from rich.console import Console

from coderazor.analyzers import AnalyzerContext, run_analyzers
from coderazor.analyze import find_cluster_flags, find_similarity_flags, merge_flags
from coderazor.artifacts import (
    layout_for,
    open_store,
    persist_sqlite_snapshot,
    save_analyze,
    save_index_units,
)
from coderazor.changesets import ChangesetStore
from coderazor.config import CoderazorConfig
from coderazor.config_store import needs_setup
from coderazor.describe import describe_units
from coderazor.document import find_document_gaps
from coderazor.embed import embed_units
from coderazor.extract import extract_workspace
from coderazor.index.hierarchy import ensure_repo_units, filter_units
from coderazor.models import AnalysisArtifacts, RunManifest, utc_now_iso
from coderazor.providers import StubProvider, build_openai_compatible_from_config
from coderazor.providers.base import LLMProvider
from coderazor.reconcile import reconcile_flags
from coderazor.report import estimate_metrics, generate_visuals, write_metrics_and_report
from coderazor.stages.document import write_doc_stub

console = Console()


def build_provider(config: CoderazorConfig) -> LLMProvider:
    if config.skip_llm:
        console.print("[yellow]Using StubProvider (--skip-llm)[/yellow]")
        return StubProvider()
    provider = build_openai_compatible_from_config(config)
    console.print(
        f"[dim]LLM {config.llm_model} @ {config.resolve_llm_base_url()} | "
        f"embed {config.embed_model} @ {config.resolve_embed_base_url()}[/dim]"
    )
    return provider


def run_analyze(config: CoderazorConfig) -> AnalysisArtifacts:
    """Index → describe? → embed → flag → reconcile → changeset queue + report."""
    if needs_setup(config):
        raise RuntimeError(
            "Configuration incomplete. Run `coderazor init` or MCP save_config "
            "(or pass --skip-llm for offline)."
        )

    layout = layout_for(config.resolve_out_dir())
    (layout.out_dir / "changesets").mkdir(parents=True, exist_ok=True)
    (layout.out_dir / "report").mkdir(parents=True, exist_ok=True)

    store = open_store(layout)
    provider = build_provider(config)
    repos = config.discover_repos()
    mode = config.resolved_describe_mode()

    console.print(f"[bold]Discovering repos in {config.workspace}[/bold] ({len(repos)} found)")
    for repo in repos:
        console.print(f"  • {repo.name}")

    console.print("[bold]Extracting code units...[/bold]")
    units = extract_workspace(repos)
    units = filter_units(units)
    units = ensure_repo_units(units)
    if not units:
        store.close()
        raise RuntimeError("No code units extracted from workspace.")
    console.print(f"Indexed {len(units)} units after filtering")
    save_index_units(layout, units)

    if mode == "embed_only":
        console.print("[yellow]describe_mode=embed_only — skipping LLM describe[/yellow]")
    else:
        console.print(f"[bold]Generating descriptions (mode={mode})...[/bold]")
        units = describe_units(units, provider, store, concurrency=config.describe_concurrency)
        if mode == "write":
            console.print("[bold]Writing in-source doc stubs for undocumented units...[/bold]")
            gaps = find_document_gaps(units, languages=config.document_language_set())
            by_id = {u.id: u for u in units}
            workspace = config.workspace.resolve()
            touched: list[str] = []
            for gap in gaps:
                unit = by_id.get(gap.unit_id)
                if not unit:
                    continue
                path = write_doc_stub(workspace, gap, unit)
                if path:
                    touched.append(path)
            console.print(f"Inserted {len(touched)} in-source doc stub(s)")

    console.print("[bold]Embedding units...[/bold]")
    units = embed_units(units, provider)

    console.print("[bold]Running analyzers...[/bold]")
    ctx = AnalyzerContext(
        workspace=config.workspace.resolve(),
        repos=repos,
        units=units,
        config=config,
    )
    analyzer_flags = run_analyzers(ctx)

    console.print("[bold]Computing similarity / clusters...[/bold]")
    sim_flags = find_similarity_flags(units, threshold=config.similarity_threshold)
    cluster_flags = find_cluster_flags(units)
    flags = merge_flags(analyzer_flags, sim_flags, cluster_flags)
    console.print(
        f"Raised {len(flags)} flags "
        f"(analyzers={len(analyzer_flags)}, similarity={len(sim_flags)}, clusters={len(cluster_flags)})"
    )

    console.print("[bold]Reconciling conflicting flags...[/bold]")
    findings, changesets = reconcile_flags(
        flags, units, provider, concurrency=config.describe_concurrency
    )

    queue_path = layout.out_dir / "changesets" / "queue.json"
    ChangesetStore(queue_path).save(changesets)
    console.print(f"[green]Wrote {len(changesets)} changesets → {queue_path}[/green]")

    metrics = estimate_metrics(
        units=units, flags=flags, findings=findings, changesets=changesets
    )
    visuals = generate_visuals(layout.out_dir / "report" / "visuals", units, changesets)
    write_metrics_and_report(
        layout.out_dir / "report",
        metrics=metrics,
        changesets=changesets,
        visual_paths=visuals,
    )

    manifest = RunManifest(
        created_at=utc_now_iso(),
        workspace=str(config.workspace.resolve()),
        repos_scanned=[p.name for p in repos],
        unit_count=len(units),
        flag_count=len(flags),
        confirmed_count=sum(1 for f in findings if f.valid),
        llm_model=config.llm_model if not config.skip_llm else "stub",
        embed_model=config.embed_model if not config.skip_llm else "stub",
        similarity_threshold=config.similarity_threshold,
        out_dir=str(layout.out_dir),
    )

    artifacts = AnalysisArtifacts(
        units=units,
        flags=flags,
        findings=findings,
        manifest=manifest,
        analyzer_notes=list(ctx.notes),
    )
    save_analyze(
        layout,
        units=units,
        flags=flags,
        findings=findings,
        manifest=manifest,
        analyzer_notes=artifacts.analyzer_notes,
    )
    persist_sqlite_snapshot(layout, artifacts)
    store.close()
    console.print(
        f"[green]Analyze complete → {layout.analyze_dir} "
        f"({manifest.confirmed_count}/{manifest.flag_count} confirmed, "
        f"{len(changesets)} changesets)[/green]"
    )
    return artifacts
