from __future__ import annotations

import json

from rich.console import Console

from autorefactor.artifacts import (
    layout_for,
    open_store,
    persist_sqlite_snapshot,
    save_analyze,
    save_index_units,
)
from autorefactor.config import MISSING_API_KEY_HINT, AutorefactorConfig
from autorefactor.config_store import needs_setup, setup_hint
from autorefactor.domain import AnalysisArtifacts, AnalysisGoal, DocumentReport, RunManifest, UnitKind, utc_now_iso
from autorefactor.enrich.coverage import enrich_coverage
from autorefactor.enrich.describe import describe_units
from autorefactor.enrich.embed import embed_units
from autorefactor.enrich.facets import attach_text_facets
from autorefactor.enrich_queue import build_enrich_tasks, pending_enrich_count, save_enrich_tasks
from autorefactor.extract import extract_workspace_cached
from autorefactor.index.hierarchy import ensure_repo_units, filter_units
from autorefactor.paths import write_run_identity
from autorefactor.plan import plan_findings
from autorefactor.plan.followups import attach_quality_followups
from autorefactor.progress import ProgressCallback, noop_progress
from autorefactor.providers import StubProvider, build_provider_from_config
from autorefactor.providers.base import LLMProvider
from autorefactor.queue import save_queue
from autorefactor.report import estimate_metrics, generate_visuals, write_metrics_and_report
from autorefactor.sensors.base import AnalysisContext
from autorefactor.sensors.collapse import collapse_findings_by_unit_set
from autorefactor.sensors.discovery import run_sensors
from autorefactor.sensors.validate import validate_findings

console = Console()


def build_provider(config: AutorefactorConfig) -> LLMProvider:
    cfg = config.apply_backend_defaults()
    if cfg.skip_llm:
        console.print("[yellow]Using StubProvider (--skip-llm)[/yellow]")
        return StubProvider()
    provider = build_provider_from_config(cfg)
    summary = cfg.runtime_summary()
    console.print(
        f"[dim]chat={summary['chat_backend']} embed={summary['embed_backend']} | "
        f"LLM {cfg.llm_model} | embed {cfg.embed_model}[/dim]"
    )
    return provider


def run_analyze(
    config: AutorefactorConfig,
    progress: ProgressCallback | None = None,
) -> AnalysisArtifacts:
    """Extract → index (describe/embed/coverage) → sensors → validate → agenda → plan → queue + report."""
    report = progress or noop_progress
    config = config.apply_backend_defaults()
    if needs_setup(config):
        raise RuntimeError(setup_hint(config) or MISSING_API_KEY_HINT)

    layout = layout_for(config.resolve_out_dir())
    for sub in ("changesets", "report", "queue", "evidence", "agenda", "enrich"):
        (layout.out_dir / sub).mkdir(parents=True, exist_ok=True)

    store = open_store(layout)
    provider = build_provider(config)
    repos = config.discover_repos()
    mode = config.resolved_describe_mode()
    focus = config.resolved_focus()
    chat_backend = config.resolved_chat_backend()
    embed_backend = config.resolved_embed_backend()

    report("discover", 5, f"{len(repos)} repos")
    console.print(f"[bold]Discovering repos in {config.workspace}[/bold] ({len(repos)} found)")
    for repo in repos:
        console.print(f"  • {repo.name}")
    console.print(
        f"[dim]focus={focus} describe_mode={mode} "
        f"chat={chat_backend} embed={embed_backend}[/dim]"
    )

    report("extract", 15, "extracting units")
    console.print("[bold]Extracting code units...[/bold]")
    units, extract_stats = extract_workspace_cached(repos, store)
    units = filter_units(units)
    units = ensure_repo_units(units)
    if not units:
        store.close()
        raise RuntimeError("No code units extracted from workspace.")
    console.print(
        f"Indexed {len(units)} units after filtering "
        f"(extract cache hits={extract_stats.get('cache_hits')} "
        f"misses={extract_stats.get('cache_misses')})"
    )
    save_index_units(layout, units)
    report(
        "extract",
        22,
        f"{len(units)} units (cache hits={extract_stats.get('cache_hits')} "
        f"misses={extract_stats.get('cache_misses')})",
    )

    report("facets", 25, "attach documentation/signature/body")
    units = attach_text_facets(config.workspace.resolve(), units)
    save_index_units(layout, units)

    document_report: DocumentReport | None = None
    describe_stats: dict[str, int] = {}
    embed_stats: dict[str, int] = {}
    # raw = embed source only; describe = HTTP describe or agent enrich for descriptions
    run_http_describe = mode == "describe" and chat_backend == "http"
    if run_http_describe:
        leaf_hint = sum(1 for u in units if u.kind not in {UnitKind.REPO, UnitKind.FILE, UnitKind.MODULE})
        report("describe", 30, f"mode={mode} units={len(units)} leaves~{leaf_hint}")
        console.print(f"[bold]Generating descriptions (mode={mode})...[/bold]")
        units = describe_units(
            units,
            provider,
            store,
            concurrency=config.describe_concurrency,
            stats=describe_stats,
        )
        report(
            "describe",
            38,
            "described "
            f"{len(units)} units mode={mode} "
            f"cache_hits={describe_stats.get('describe_cache_hits', 0)} "
            f"llm_calls={describe_stats.get('describe_llm_calls', 0)}",
        )
    else:
        reason = "agent enrich" if (mode == "describe" and chat_backend == "agent") else mode
        console.print(f"[yellow]Skipping in-process LLM describe ({reason})[/yellow]")
        report("describe", 30, f"skipped ({reason})")

    enabled = {p.strip() for p in config.enabled_sensors().split(",") if p.strip()}
    embed_sensors = {"sim.method", "sim.class", "placement", "cluster"}
    needs_embed = embed_backend in {"http", "stub"} and (
        bool(enabled & embed_sensors) if enabled else True
    )
    if needs_embed:
        report("embed", 45, f"embedding {len(units)} units (cached when possible)")
        console.print("[bold]Embedding units...[/bold]")
        cfg_ready = config.apply_backend_defaults()

        def _embed_progress(done: int, total: int) -> None:
            frac = done / max(total, 1)
            report(
                "embed",
                45 + (7 * frac),
                f"embedded {done}/{total} view-texts",
            )

        units = embed_units(
            units,
            provider,
            store,
            embed_model=cfg_ready.embed_model,
            describe_mode=mode,
            batch_size=cfg_ready.embed_batch_size,
            concurrency=int(getattr(cfg_ready, "embed_concurrency", 1) or 1),
            stats=embed_stats,
            on_progress=_embed_progress,
        )
        report(
            "embed",
            52,
            "embedded "
            f"{len(units)} units cache_hits={embed_stats.get('embed_cache_hits', 0)} "
            f"batches={embed_stats.get('embed_batches_sent', 0)}",
        )
    else:
        report("embed", 45, "skipped (embed_backend=none or no embed sensors)")
        console.print(
            f"[yellow]Skipping embeddings — embed_backend={embed_backend}[/yellow]"
        )

    report("coverage", 55, "coverage index")
    console.print("[bold]Indexing coverage...[/bold]")
    coverage = enrich_coverage(
        workspace=config.workspace.resolve(),
        repos=repos,
        out_path=layout.out_dir / "index" / "coverage.json",
    )
    cov_files = len(getattr(coverage, "files", None) or [])
    report("coverage", 60, f"coverage files={cov_files}")

    ctx = AnalysisContext(
        workspace=config.workspace.resolve(),
        repos=tuple(repos),
        units=tuple(units),
        config=config,
        focus=focus,
        coverage=coverage,
    )
    report("sensors", 65, "fingerprint + embeddings + smells")
    console.print("[bold]Running sensors...[/bold]")
    raw_findings, sensor_reports = run_sensors(ctx, concurrency=config.describe_concurrency)
    findings, rejected = validate_findings(raw_findings, ctx.known_ids())
    before_collapse = len(findings)
    findings = collapse_findings_by_unit_set(findings)
    if before_collapse != len(findings):
        console.print(
            f"[dim]Collapsed findings {before_collapse} → {len(findings)} "
            f"(clone prefer / split demote)[/dim]"
        )
    if config.max_flags is not None and config.max_flags > 0:
        findings = sorted(findings, key=lambda f: -f.score)[: config.max_flags]

    (layout.out_dir / "evidence" / "findings.jsonl").write_text(
        "\n".join(f.model_dump_json() for f in findings) + ("\n" if findings else ""),
        encoding="utf-8",
    )

    console.print(
        f"Raised {len(raw_findings)} findings → kept {len(findings)} "
        f"(structural rejects {len(rejected)}; sensors="
        + ", ".join(f"{r.sensor}:{r.finding_count}" for r in sensor_reports if r.status == "ran")
        + ")"
    )
    report(
        "sensors",
        75,
        f"findings={len(findings)} raw={len(raw_findings)} rejected={len(rejected)}",
    )

    # Planning: HTTP chat uses real provider; agent/stub uses StubProvider heuristics
    plan_provider: LLMProvider = provider
    if chat_backend in {"agent", "stub"}:
        plan_provider = StubProvider()
        console.print("[yellow]Planning with heuristic stub (agent/sequential enrich queue)[/yellow]")

    report("plan", 80, f"agenda / planner findings={len(findings)}")
    console.print("[bold]Planning agenda components...[/bold]")
    confirmed, changesets, decisions, agenda = plan_findings(
        findings,
        units,
        plan_provider,
        concurrency=config.describe_concurrency if chat_backend == "http" else 1,
        max_component_size=max(1, int(config.max_plan_component_size or 8)),
    )
    changesets = [cs for cs in changesets if cs.targets]
    coverage_finding_count = sum(1 for f in findings if f.goal == AnalysisGoal.COVERAGE)
    changesets = attach_quality_followups(
        changesets,
        units,
        coverage=coverage,
        coverage_threshold=float(getattr(config, "coverage_threshold", 80.0) or 80.0),
        workspace=config.workspace,
        repos=repos,
        coverage_finding_count=coverage_finding_count,
    )
    report("plan", 90, f"changesets={len(changesets)} confirmed={sum(1 for c in confirmed if c.valid)}")

    enrich_pending = 0
    if chat_backend == "agent":
        # Always enqueue plan tasks; describe enrich only when mode=describe
        enrich_tasks = build_enrich_tasks(
            findings=findings,
            units=units,
            max_describe=int(config.max_enrich_describe or 40) if mode == "describe" else 0,
            include_plan=True,
            max_plan_component_size=max(1, int(config.max_plan_component_size or 8)),
        )
        save_enrich_tasks(layout.out_dir, enrich_tasks)
        enrich_pending = pending_enrich_count(layout.out_dir)
        console.print(
            f"[green]Enqueued {len(enrich_tasks)} enrich tasks "
            f"({enrich_pending} pending) for host agent[/green]"
        )
    else:
        save_enrich_tasks(layout.out_dir, [])

    (layout.out_dir / "agenda" / "agenda.json").write_text(
        agenda.model_dump_json(indent=2), encoding="utf-8"
    )
    from autorefactor.agenda import conflict_components, split_oversized_components

    components_payload = {
        "components": [
            {"finding_ids": [f.id for f in c]}
            for c in split_oversized_components(
                conflict_components(findings, {u.id: u for u in units}),
                max_size=max(1, int(config.max_plan_component_size or 8)),
            )
        ]
    }
    (layout.out_dir / "agenda" / "components.json").write_text(
        json.dumps(components_payload, indent=2), encoding="utf-8"
    )

    queue_path = save_queue(layout.out_dir, changesets)
    console.print(f"[green]Wrote {len(changesets)} changesets → {queue_path}[/green]")

    quality_warnings = []
    for r in sensor_reports:
        if r.status == "skipped":
            quality_warnings.append(f"Sensor '{r.sensor}' skipped: {r.detail or 'unavailable'}")
        elif r.status == "error":
            quality_warnings.append(f"Sensor '{r.sensor}' error: {r.detail}")
    if rejected:
        quality_warnings.append(
            f"Dropped {len(rejected)} malformed findings (empty/unresolved targets only)."
        )
    if chat_backend == "agent":
        quality_warnings.append(
            "chat_backend=agent: drain enrich_tasks via get_next_enrich_task before relying on briefs."
        )

    runtime = config.runtime_summary()
    metrics = estimate_metrics(
        units=units,
        findings=findings,
        confirmed=confirmed,
        changesets=changesets,
        auto_rejected_count=len(rejected),
        quality_warnings=quality_warnings,
        raw_finding_count=len(raw_findings),
        coverage=coverage,
        workspace=config.workspace,
        repos=repos,
        pipeline_stats={
            "runtime": runtime,
            "extract": extract_stats,
            "describe": describe_stats,
            "embed": embed_stats,
        },
    )
    visuals = generate_visuals(
        layout.out_dir / "report" / "visuals",
        units,
        changesets,
        agenda=agenda,
        metrics=metrics,
        report_dir=layout.out_dir / "report",
    )
    write_metrics_and_report(
        layout.out_dir / "report",
        metrics=metrics,
        changesets=changesets,
        visual_paths=visuals,
    )

    sensors_ran = [r.sensor for r in sensor_reports if r.status == "ran"]
    llm_label = config.llm_model
    embed_label = config.embed_model
    if config.skip_llm:
        llm_label = "stub"
        embed_label = "stub"
    elif chat_backend == "agent":
        llm_label = "agent"
    if embed_backend == "none":
        embed_label = "none"

    write_run_identity(layout.out_dir, config.workspace.resolve())

    manifest = RunManifest(
        created_at=utc_now_iso(),
        workspace=str(config.workspace.resolve()),
        repos_scanned=[p.name for p in repos],
        unit_count=len(units),
        finding_count=len(findings),
        confirmed_count=sum(1 for c in confirmed if c.valid),
        llm_model=llm_label,
        embed_model=embed_label,
        similarity_threshold=config.similarity_threshold,
        out_dir=str(layout.out_dir),
        focus=focus,
        sensors_ran=sensors_ran,
        hints_enabled=[p.strip() for p in (config.hints or "").split(",") if p.strip()],
        chat_backend=chat_backend,
        embed_backend=embed_backend,
        describe_mode=mode,
    )
    (layout.out_dir / "manifest.json").write_text(
        manifest.model_dump_json(indent=2), encoding="utf-8"
    )

    artifacts = AnalysisArtifacts(
        units=units,
        findings=findings,
        confirmed=confirmed,
        agenda=agenda,
        manifest=manifest,
        document_report=document_report,
        coverage=coverage,
        sensor_reports=sensor_reports,
    )
    save_analyze(
        layout,
        units=units,
        findings=findings,
        confirmed=confirmed,
        manifest=manifest,
        sensor_reports=sensor_reports,
    )
    persist_sqlite_snapshot(layout, artifacts)
    store.close()
    report("done", 100, f"{len(changesets)} changesets enrich_pending={enrich_pending}")
    console.print(
        f"[green]Analyze complete → {layout.out_dir} "
        f"({manifest.confirmed_count}/{manifest.finding_count} confirmed, "
        f"{len(changesets)} changesets, schema_version={manifest.schema_version})[/green]"
    )
    return artifacts
