from __future__ import annotations

from pathlib import Path

from rich.console import Console

from coderazor.artifacts import layout_for, load_artifacts, persist_sqlite_snapshot, save_document
from coderazor.config import CoderazorConfig
from coderazor.document import find_document_gaps
from coderazor.models import DocumentGap, DocumentReport

console = Console()


def _insert_after_signature(path: Path, simple_name: str, doc_line: str, *, patterns: list[str]) -> bool:
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if not any(p.format(name=simple_name) in line for p in patterns):
            continue
        # Already documented?
        window = "".join(lines[max(0, i - 3) : i + 3])
        if '"""' in window or "/**" in window or "///" in window:
            return False
        indent = ""
        if i + 1 < len(lines):
            raw = lines[i + 1]
            indent = raw[: len(raw) - len(raw.lstrip())]
        insertion = doc_line if doc_line.endswith("\n") else doc_line + "\n"
        if "{INDENT}" in insertion:
            insertion = insertion.replace("{INDENT}", indent or "    ")
            lines.insert(i + 1, insertion)
        else:
            # Prefixed docs (Javadoc) go before the signature
            prefix_indent = line[: len(line) - len(line.lstrip())]
            prefixed = insertion.replace("{PREFIX}", prefix_indent)
            lines.insert(i, prefixed)
        path.write_text("".join(lines), encoding="utf-8")
        return True
    return False


def write_doc_stub(workspace: Path, gap: DocumentGap, unit) -> str | None:
    """Deterministic in-source doc stub (no OpenHands)."""
    path = workspace / gap.repo / gap.path
    simple = unit.qualified_name.rsplit(".", 1)[-1]
    lang = (gap.language or "").lower()
    if lang == "python":
        ok = _insert_after_signature(
            path,
            simple,
            '{INDENT}"""TODO: characterize {qn}."""\n'.format(qn=unit.qualified_name),
            patterns=["def {name}", "class {name}"],
        )
    elif lang == "java":
        ok = _insert_after_signature(
            path,
            simple,
            "{PREFIX}/** TODO: characterize {qn}. */\n".format(qn=unit.qualified_name),
            patterns=["class {name}", "interface {name}", " {name}(", "void {name}(", "public "],
        )
        # Java patterns are loose; prefer method/class name token
        if not ok:
            ok = _insert_after_signature(
                path,
                simple,
                "{PREFIX}/** TODO: characterize {qn}. */\n".format(qn=unit.qualified_name),
                patterns=["{name}("],
            )
    elif lang in {"javascript", "typescript", "tsx"}:
        ok = _insert_after_signature(
            path,
            simple,
            "{PREFIX}/** TODO: characterize {qn}. */\n".format(qn=unit.qualified_name),
            patterns=["function {name}", "class {name}", "{name}(", "const {name}", "export function {name}"],
        )
    elif lang == "go":
        ok = _insert_after_signature(
            path,
            simple,
            "{PREFIX}// TODO: characterize {qn}.\n".format(qn=unit.qualified_name),
            patterns=["func {name}", "type {name}", "func ("],
        )
        if not ok:
            ok = _insert_after_signature(
                path,
                simple,
                "{PREFIX}// TODO: characterize {qn}.\n".format(qn=unit.qualified_name),
                patterns=["{name}"],
            )
    else:
        return None
    return f"{gap.repo}/{gap.path}" if ok else None


def run_document(config: CoderazorConfig) -> DocumentReport:
    """Detect missing docs and insert deterministic stubs (host agent for polish)."""
    layout = layout_for(config.resolve_out_dir())
    artifacts = load_artifacts(layout.out_dir)
    languages = config.document_language_set()
    gaps = find_document_gaps(artifacts.units, languages=languages)
    console.print(f"[bold]Document gaps:[/bold] {len(gaps)}")

    if not gaps:
        report = DocumentReport(
            summary="No missing in-source documentation detected for configured languages.",
            gaps=[],
            touched_paths=[],
            skipped=False,
        )
        save_document(layout, report)
        artifacts.document_report = report
        persist_sqlite_snapshot(layout, artifacts)
        return report

    workspace = config.workspace.resolve()
    (layout.document_dir / "document-brief.md").write_text(
        "# coderazor document gaps\n\n"
        + "\n".join(
            f"- `{g.repo}/{g.path}` :: `{g.qualified_name}` ({g.language})" for g in gaps[:80]
        )
        + "\n",
        encoding="utf-8",
    )

    by_id = {u.id: u for u in artifacts.units}
    touched: list[str] = []
    for gap in gaps:
        unit = by_id.get(gap.unit_id)
        if not unit:
            continue
        path = write_doc_stub(workspace, gap, unit)
        if path:
            touched.append(path)

    report = DocumentReport(
        summary=(
            f"Document stage: {len(gaps)} gap(s); inserted {len(touched)} stub(s). "
            "Host agent may refine documentation."
        ),
        gaps=gaps,
        touched_paths=touched,
        skipped=False,
    )
    save_document(layout, report)
    artifacts.document_report = report
    persist_sqlite_snapshot(layout, artifacts)
    console.print(f"[green]Document report → {layout.document_dir}[/green]")
    return report
