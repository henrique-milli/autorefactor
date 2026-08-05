from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

from coderazor import __version__
from coderazor.changesets import ChangesetStatus, ChangesetStore
from coderazor.config import CoderazorConfig
from coderazor.config_store import (
    get_setup_requirements,
    load_merged_config,
    needs_setup,
    save_config,
)
from coderazor.stages import run_analyze, run_document, run_index

app = typer.Typer(
    name="coderazor",
    help="coderazor.ai — analysis engine + changeset queue for host agents",
    add_completion=False,
    no_args_is_help=True,
)
changesets_app = typer.Typer(help="Manage the changeset queue")
app.add_typer(changesets_app, name="changesets")
console = Console()


def _build_config(
    workspace: Path,
    out: Path,
    *,
    base_url: Optional[str] = None,
    llm_model: Optional[str] = None,
    embed_model: Optional[str] = None,
    threshold: Optional[float] = None,
    skip_llm: bool = False,
    skip_describe: bool = False,
    analyzers: Optional[str] = None,
    describe_mode: Optional[str] = None,
) -> CoderazorConfig:
    overrides = {
        "workspace": str(workspace),
        "out_dir": str(out),
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
    if analyzers is not None:
        overrides["analyzers"] = analyzers
    if describe_mode is not None:
        overrides["describe_mode"] = describe_mode
    config = load_merged_config(
        workspace=workspace, out_dir=out, cli_overrides=overrides
    )
    config.skip_llm = skip_llm or config.skip_llm
    config.skip_describe = skip_describe or config.skip_describe
    if describe_mode:
        config.describe_mode = describe_mode
    if analyzers:
        config.analyzers = analyzers
    return config


@app.callback()
def main() -> None:
    """coderazor.ai CLI."""


@app.command("version")
def version_cmd() -> None:
    console.print(__version__)


@app.command("init")
def init_cmd(
    workspace: Path = typer.Option(Path("."), "--workspace", "-w"),
    user_level: bool = typer.Option(False, "--user", help="Write ~/.config/coderazor/config.toml"),
) -> None:
    """Guided setup — writes .coderazor.toml (once)."""
    console.print("[bold]coderazor init[/bold]")
    base_url = typer.prompt("OpenAI-compatible base URL", default="https://api.openai.com/v1")
    api_key = typer.prompt("API key (chat+embed, leave empty for local/no-auth)", default="", hide_input=True)
    llm_model = typer.prompt("Chat model", default="gpt-4o-mini")
    embed_model = typer.prompt("Embedding model", default="text-embedding-3-small")
    describe_mode = typer.prompt("describe_mode (embed_only|index|write)", default="index")
    confidence = float(typer.prompt("confidence_threshold", default="0.6"))
    out_dir = typer.prompt("Default out dir", default="out")
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
    out: Path = typer.Option(Path("out"), "--out", "-o"),
) -> None:
    """Discover repos and extract code units (no LLM)."""
    config = CoderazorConfig(workspace=workspace, out_dir=out)
    units = run_index(config)
    console.print(f"Indexed {len(units)} units")


@app.command("analyze")
def analyze_cmd(
    workspace: Path = typer.Option(Path("."), "--workspace", "-w"),
    out: Path = typer.Option(Path("out"), "--out", "-o"),
    base_url: Optional[str] = typer.Option(None, "--base-url"),
    llm_model: Optional[str] = typer.Option(None, "--llm-model"),
    embed_model: Optional[str] = typer.Option(None, "--embed-model"),
    threshold: Optional[float] = typer.Option(None, "--threshold"),
    skip_llm: bool = typer.Option(False, "--skip-llm"),
    skip_describe: bool = typer.Option(False, "--skip-describe"),
    analyzers: Optional[str] = typer.Option(None, "--analyzers"),
    describe_mode: Optional[str] = typer.Option(None, "--describe-mode"),
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
        analyzers=analyzers,
        describe_mode=describe_mode,
    )
    try:
        artifacts = run_analyze(config)
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")
        for req in get_setup_requirements(config):
            console.print(f"  - {req['field']}: {req['question']}")
        raise typer.Exit(code=1) from exc
    if artifacts.manifest:
        console.print(
            f"Confirmed {artifacts.manifest.confirmed_count}/{artifacts.manifest.flag_count} flags"
        )
    console.print(f"Report: {out}/report/report.md")
    console.print(f"Queue: {out}/changesets/queue.json")


@app.command("document")
def document_cmd(
    workspace: Path = typer.Option(Path("."), "--workspace", "-w"),
    out: Path = typer.Option(Path("out"), "--out", "-o"),
) -> None:
    """Detect missing docs and insert deterministic language stubs."""
    config = _build_config(workspace, out, skip_llm=True)
    report = run_document(config)
    console.print(report.summary)


@app.command("status")
def status_cmd(
    out: Path = typer.Option(Path("out"), "--out", "-o"),
    workspace: Path = typer.Option(Path("."), "--workspace", "-w"),
) -> None:
    """Show setup + queue progress."""
    config = load_merged_config(workspace=workspace, out_dir=out)
    console.print(f"needs_setup={needs_setup(config)} describe_mode={config.resolved_describe_mode()}")
    store = ChangesetStore(out / "changesets" / "queue.json")
    items = store.list()
    by_status: dict[str, int] = {}
    for item in items:
        by_status[item.status.value] = by_status.get(item.status.value, 0) + 1
    console.print(f"changesets={len(items)} {by_status}")
    report = out / "report" / "report.md"
    console.print(f"report={'yes' if report.exists() else 'no'} ({report})")


@app.command("report")
def report_cmd(out: Path = typer.Option(Path("out"), "--out", "-o")) -> None:
    """Print path to report.md and metric highlights."""
    report = out / "report" / "report.md"
    metrics = out / "report" / "metrics.json"
    if report.exists():
        console.print(report.read_text(encoding="utf-8")[:2000])
    else:
        console.print("[yellow]No report yet — run coderazor analyze[/yellow]")
    if metrics.exists():
        console.print(f"\nmetrics: {metrics}")


@changesets_app.command("list")
def changesets_list(out: Path = typer.Option(Path("out"), "--out", "-o")) -> None:
    store = ChangesetStore(out / "changesets" / "queue.json")
    for item in store.list():
        console.print(
            f"{item.id} [{item.status.value}] conf={item.confidence:.2f} {item.action} — {item.title}"
        )


@changesets_app.command("next")
def changesets_next(
    out: Path = typer.Option(Path("out"), "--out", "-o"),
    min_confidence: Optional[float] = typer.Option(None, "--min-confidence"),
) -> None:
    config = load_merged_config()
    threshold = config.confidence_threshold if min_confidence is None else min_confidence
    result = ChangesetStore(out / "changesets" / "queue.json").next_pending(
        min_confidence=threshold
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
    out: Path = typer.Option(Path("out"), "--out", "-o"),
    notes: str = typer.Option("", "--notes"),
) -> None:
    updated = ChangesetStore(out / "changesets" / "queue.json").mark(
        changeset_id, ChangesetStatus(status), notes=notes
    )
    console.print(f"Marked {updated.id} → {updated.status.value}")


if __name__ == "__main__":
    app()
