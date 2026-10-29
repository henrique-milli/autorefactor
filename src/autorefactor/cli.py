from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

from autorefactor import __version__
from autorefactor.changesets import ChangesetStatus, ChangesetStore
from autorefactor.config import AutorefactorConfig
from autorefactor.config_store import (
    get_setup_requirements,
    load_merged_config,
    needs_setup,
    save_config,
)
from autorefactor.paths import default_run_dir, resolve_out_dir
from autorefactor.stages import run_analyze, run_document, run_index

app = typer.Typer(
    name="autorefactor",
    help="autorefactor.ai — analysis engine + changeset queue for host agents",
    add_completion=False,
    no_args_is_help=True,
)
changesets_app = typer.Typer(help="Manage the changeset queue")
app.add_typer(changesets_app, name="changesets")
console = Console()


def _build_config(
    workspace: Path,
    out: Path | None,
    *,
    base_url: Optional[str] = None,
    llm_model: Optional[str] = None,
    embed_model: Optional[str] = None,
    threshold: Optional[float] = None,
    skip_llm: bool = False,
    skip_describe: bool = False,
    sensors: Optional[str] = None,
    describe_mode: Optional[str] = None,
    focus: Optional[str] = None,
) -> AutorefactorConfig:
    resolved_out = resolve_out_dir(workspace, out)
    overrides = {
        "workspace": str(workspace),
        "out_dir": str(resolved_out),
        "skip_llm": skip_llm,
    }
    if base_url:
        overrides["base_url"] = base_url
    if llm_model:
        overrides["llm_model"] = llm_model
    if embed_model:
        overrides["embed_model"] = embed_model
    if threshold is not None:
        overrides["similarity_threshold"] = threshold
    if sensors is not None:
        overrides["sensors"] = sensors
    if describe_mode is not None:
        overrides["describe_mode"] = describe_mode
    if focus is not None:
        overrides["focus"] = focus
    config = load_merged_config(
        workspace=workspace, out_dir=resolved_out, cli_overrides=overrides
    )
    config.skip_llm = skip_llm or config.skip_llm
    config.skip_describe = skip_describe or config.skip_describe
    if describe_mode:
        config.describe_mode = describe_mode
    if sensors:
        config.sensors = sensors
    if focus:
        config.focus = focus
    return config


@app.callback()
def main() -> None:
    """autorefactor.ai CLI."""


@app.command("sync")
def sync_cmd(
    workspace: Path = typer.Option(Path("."), "--workspace", "-w", help="Project working tree to upload"),
    url: str = typer.Option(
        "http://127.0.0.1:8765/v1/workspace/sync",
        "--url",
        help="Remote MCP sync endpoint",
    ),
    label: Optional[str] = typer.Option(None, "--label", help="Optional label stored with the sync"),
) -> None:
    """Upload working-tree files (incl. uncommitted) to a remote autorefactor MCP server."""
    from autorefactor.sync_client import sync_workspace_http

    console.print(f"Packing {workspace.resolve()} …")
    try:
        result = sync_workspace_http(
            workspace,
            url=url,
            label=label,
            source=str(workspace.resolve()),
        )
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    console.print(
        f"[green]Synced[/green] files={result.get('file_count')} "
        f"bytes={result.get('uploaded_bytes')} → {result.get('root')}"
    )


@app.command("prepare")
def prepare_cmd(
    git_url: Optional[str] = typer.Option(None, "--git-url", help="Remote git URL (https/ssh)"),
    git_urls: Optional[list[str]] = typer.Option(
        None, "--git-urls", help="Multiple repo URLs (repeatable) for cross-repo active workspace"
    ),
    ref: Optional[str] = typer.Option(None, "--ref", help="Branch or tag"),
) -> None:
    """Clone one or more git repos into ~/.autorefactor/workspaces/active."""
    from autorefactor.workspace_sync import clone_git_workspace, clone_git_workspaces

    urls = list(git_urls or [])
    if git_url:
        urls = [git_url, *urls]
    if not urls:
        console.print("[red]Provide --git-url and/or --git-urls[/red]")
        raise typer.Exit(code=1)
    console.print(f"Cloning {len(urls)} repo(s) …")
    try:
        result = (
            clone_git_workspace(urls[0], ref=ref)
            if len(urls) == 1
            else clone_git_workspaces(urls, ref=ref)
        )
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    console.print(
        f"[green]Prepared[/green] files={result.get('file_count')} → {result.get('root')}"
    )


@app.command("version")
def version_cmd() -> None:
    console.print(__version__)


@app.command("init")
def init_cmd(
    workspace: Path = typer.Option(Path("."), "--workspace", "-w"),
    user_level: bool = typer.Option(False, "--user", help="Write ~/.config/autorefactor/config.toml"),
) -> None:
    """Guided setup — writes .autorefactor.toml (once)."""
    console.print("[bold]autorefactor init[/bold]")
    base_url = typer.prompt("OpenAI-compatible base URL", default="https://api.openai.com/v1")
    api_key = typer.prompt("API key (chat+embed, leave empty for local/no-auth)", default="", hide_input=True)
    llm_model = typer.prompt("Chat model", default="gpt-6-luna")
    embed_model = typer.prompt("Embedding model", default="text-embedding-3-small")
    describe_mode = typer.prompt("describe_mode (raw|describe)", default="raw")
    confidence = float(typer.prompt("confidence_threshold", default="0.6"))
    suggested = str(default_run_dir(workspace, create=False))
    out_dir = typer.prompt("Default out dir", default=suggested)
    partial = {
        "workspace": str(workspace.resolve()),
        "out_dir": out_dir,
        "base_url": base_url,
        "api_key": api_key or None,
        "llm_model": llm_model,
        "embed_model": embed_model,
        "describe_mode": describe_mode,
        "confidence_threshold": confidence,
    }
    path = save_config(partial, workspace=workspace, user_level=user_level)
    console.print(f"[green]Wrote {path}[/green]")


@app.command("index")
def index_cmd(
    workspace: Path = typer.Option(Path("."), "--workspace", "-w"),
    out: Optional[Path] = typer.Option(
        None, "--out", "-o", help="Artifact dir (default: ~/.autorefactor/runs/<slug>/)"
    ),
) -> None:
    """Discover repos and extract code units (no LLM)."""
    resolved = resolve_out_dir(workspace, out)
    config = AutorefactorConfig(workspace=workspace, out_dir=resolved)
    units = run_index(config)
    console.print(f"Indexed {len(units)} units → {resolved}")


@app.command("analyze")
def analyze_cmd(
    workspace: Path = typer.Option(Path("."), "--workspace", "-w"),
    out: Optional[Path] = typer.Option(
        None, "--out", "-o", help="Artifact dir (default: ~/.autorefactor/runs/<slug>/)"
    ),
    base_url: Optional[str] = typer.Option(None, "--base-url"),
    llm_model: Optional[str] = typer.Option(None, "--llm-model"),
    embed_model: Optional[str] = typer.Option(None, "--embed-model"),
    threshold: Optional[float] = typer.Option(None, "--threshold"),
    skip_llm: bool = typer.Option(False, "--skip-llm"),
    skip_describe: bool = typer.Option(False, "--skip-describe"),
    sensors: Optional[str] = typer.Option(None, "--sensors", help="auto|full or comma-separated sensor ids"),
    describe_mode: Optional[str] = typer.Option(None, "--describe-mode"),
    focus: Optional[str] = typer.Option(
        None, "--focus", help="balanced|strict|cross_repo|smells|clones"
    ),
) -> None:
    """Run analysis → report + changeset queue."""
    config = _build_config(
        workspace,
        out,
        base_url=base_url,
        llm_model=llm_model,
        embed_model=embed_model,
        threshold=threshold,
        skip_llm=skip_llm,
        skip_describe=skip_describe,
        sensors=sensors,
        describe_mode=describe_mode,
        focus=focus,
    )
    out_resolved = config.resolve_out_dir()
    try:
        artifacts = run_analyze(config)
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")
        for req in get_setup_requirements(config):
            console.print(f"  - {req['field']}: {req['question']}")
        raise typer.Exit(code=1) from exc
    if artifacts.manifest:
        console.print(
            f"Confirmed {artifacts.manifest.confirmed_count}/{artifacts.manifest.finding_count} findings"
        )
    console.print(f"Report: {out_resolved}/report/report.md")
    console.print(
        f"Queue: {out_resolved}/queue/changesets.json (compat: {out_resolved}/changesets/queue.json)"
    )


@app.command("document")
def document_cmd(
    workspace: Path = typer.Option(Path("."), "--workspace", "-w"),
    out: Optional[Path] = typer.Option(
        None, "--out", "-o", help="Artifact dir (default: ~/.autorefactor/runs/<slug>/)"
    ),
) -> None:
    """Detect missing docs and insert deterministic language stubs."""
    config = _build_config(workspace, out, skip_llm=True)
    report = run_document(config)
    console.print(report.summary)


@app.command("status")
def status_cmd(
    workspace: Path = typer.Option(Path("."), "--workspace", "-w"),
    out: Optional[Path] = typer.Option(
        None, "--out", "-o", help="Artifact dir (default: ~/.autorefactor/runs/<slug>/)"
    ),
) -> None:
    """Show setup + queue progress."""
    resolved = resolve_out_dir(workspace, out)
    config = load_merged_config(workspace=workspace, out_dir=resolved)
    console.print(f"needs_setup={needs_setup(config)} describe_mode={config.resolved_describe_mode()}")
    console.print(f"out_dir={resolved}")
    store = ChangesetStore(resolved / "changesets" / "queue.json")
    items = store.list()
    by_status: dict[str, int] = {}
    for item in items:
        by_status[item.status.value] = by_status.get(item.status.value, 0) + 1
    console.print(f"changesets={len(items)} {by_status}")
    report = resolved / "report" / "report.md"
    console.print(f"report={'yes' if report.exists() else 'no'} ({report})")


@app.command("report")
def report_cmd(
    workspace: Path = typer.Option(Path("."), "--workspace", "-w"),
    out: Optional[Path] = typer.Option(
        None, "--out", "-o", help="Artifact dir (default: ~/.autorefactor/runs/<slug>/)"
    ),
) -> None:
    """Print path to report.md and metric highlights."""
    resolved = resolve_out_dir(workspace, out)
    report = resolved / "report" / "report.md"
    metrics = resolved / "report" / "metrics.json"
    if report.exists():
        console.print(report.read_text(encoding="utf-8")[:2000])
    else:
        console.print("[yellow]No report yet — run autorefactor analyze[/yellow]")
    if metrics.exists():
        console.print(f"\nmetrics: {metrics}")


@changesets_app.command("list")
def changesets_list(
    workspace: Path = typer.Option(Path("."), "--workspace", "-w"),
    out: Optional[Path] = typer.Option(
        None, "--out", "-o", help="Artifact dir (default: ~/.autorefactor/runs/<slug>/)"
    ),
) -> None:
    resolved = resolve_out_dir(workspace, out)
    store = ChangesetStore(resolved / "changesets" / "queue.json")
    for item in store.list():
        console.print(
            f"{item.id} [{item.status.value}] conf={item.confidence:.2f} {item.action} — {item.title}"
        )


@changesets_app.command("next")
def changesets_next(
    workspace: Path = typer.Option(Path("."), "--workspace", "-w"),
    out: Optional[Path] = typer.Option(
        None, "--out", "-o", help="Artifact dir (default: ~/.autorefactor/runs/<slug>/)"
    ),
    min_confidence: Optional[float] = typer.Option(None, "--min-confidence"),
) -> None:
    from autorefactor.queue import next_for_display

    resolved = resolve_out_dir(workspace, out)
    config = load_merged_config(workspace=workspace, out_dir=resolved)
    threshold = config.confidence_threshold if min_confidence is None else min_confidence
    result = next_for_display(
        resolved,
        min_confidence=threshold,
        display_limit=config.queue_display_limit,
    )
    if result.done:
        console.print("Queue empty.")
        return
    assert result.changeset is not None
    if result.needs_confirmation:
        console.print("[yellow]needs_confirmation[/yellow]")
        for q in result.questions:
            console.print(f"- {q}")
    console.print(result.changeset.brief)


@changesets_app.command("mark")
def changesets_mark(
    changeset_id: str = typer.Argument(...),
    status: str = typer.Argument(...),
    workspace: Path = typer.Option(Path("."), "--workspace", "-w"),
    out: Optional[Path] = typer.Option(
        None, "--out", "-o", help="Artifact dir (default: ~/.autorefactor/runs/<slug>/)"
    ),
    notes: str = typer.Option("", "--notes"),
) -> None:
    resolved = resolve_out_dir(workspace, out)
    updated = ChangesetStore(resolved / "changesets" / "queue.json").mark(
        changeset_id, ChangesetStatus(status), notes=notes
    )
    console.print(f"Marked {updated.id} → {updated.status.value}")


if __name__ == "__main__":
    app()
