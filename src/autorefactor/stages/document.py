from __future__ import annotations

import re
from pathlib import Path

from rich.console import Console

from autorefactor.artifacts import layout_for, load_artifacts, persist_sqlite_snapshot, save_document
from autorefactor.config import AutorefactorConfig
from autorefactor.document import find_document_gaps, get_pack, is_replaceable_doc
from autorefactor.document.packs.base import AUTOREFACTOR_DOC_MARKER, ensure_marker
from autorefactor.models import DocumentGap, DocumentReport
from autorefactor.paths import resolve_repo_file

console = Console()

_ANNOTATION_LANGS = frozenset({"java", "javascript", "typescript", "tsx", "jsx"})


def _patterns_for(lang: str) -> list[str]:
    if lang == "python":
        return ["def {name}", "class {name}"]
    if lang == "java":
        return ["class {name}", "interface {name}", " {name}(", "void {name}(", "{name}("]
    if lang in {"javascript", "typescript", "tsx", "jsx"}:
        return ["function {name}", "class {name}", "{name}(", "const {name}", "export function {name}"]
    if lang == "go":
        return ["func {name}", "type {name}", "func (", "{name}"]
    return []


def _find_signature_line(lines: list[str], simple_name: str, patterns: list[str]) -> int | None:
    for i, line in enumerate(lines):
        if any(p.format(name=simple_name) in line for p in patterns):
            return i
    return None


def _annotation_block_start(lines: list[str], sig_idx: int, lang: str) -> int:
    """First line of contiguous annotations immediately above the signature."""
    if lang not in _ANNOTATION_LANGS:
        return sig_idx
    i = sig_idx - 1
    while i >= 0 and not lines[i].strip():
        i -= 1
    head = sig_idx
    paren = 0
    while i >= 0:
        stripped = lines[i].strip()
        if not stripped:
            i -= 1
            continue
        paren += stripped.count(")") - stripped.count("(")
        if stripped.startswith("@") or paren > 0:
            head = i
            i -= 1
            continue
        break
    return head


def _remove_leading_doc(lines: list[str], anchor_idx: int, lang: str) -> int:
    """Remove replaceable doc immediately before (or docstring after) anchor. Return new anchor."""
    if lang == "python":
        # Docstring is after signature line
        j = anchor_idx + 1
        while j < len(lines) and not lines[j].strip():
            j += 1
        if j < len(lines) and ('"""' in lines[j] or "'''" in lines[j]):
            quote = '"""' if '"""' in lines[j] else "'''"
            if lines[j].count(quote) >= 2:
                del lines[j]
                return anchor_idx
            end = j + 1
            while end < len(lines) and quote not in lines[end]:
                end += 1
            if end < len(lines):
                del lines[j : end + 1]
            return anchor_idx
        return anchor_idx

    # Prefixed block comments / // comments before anchor
    start = anchor_idx
    i = anchor_idx - 1
    while i >= 0 and not lines[i].strip():
        i -= 1
    if lang == "go":
        while i >= 0 and lines[i].lstrip().startswith("//"):
            start = i
            i -= 1
        if start < anchor_idx:
            del lines[start:anchor_idx]
            return start
        return anchor_idx

    # java / js: /** ... */
    if i >= 0 and "*/" in lines[i]:
        end = i
        while i >= 0 and "/**" not in lines[i]:
            i -= 1
        if i >= 0 and "/**" in lines[i]:
            del lines[i : end + 1]
            # also drop blank lines left between doc and anchor
            while i < len(lines) and i < anchor_idx and not lines[i].strip():
                del lines[i]
                anchor_idx -= 1
            return i
    return anchor_idx


def _insert_doc_at_signature(
    path: Path,
    simple_name: str,
    wrapped: str,
    *,
    patterns: list[str],
    lang: str,
    replace: bool,
) -> bool:
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    sig_idx = _find_signature_line(lines, simple_name, patterns)
    if sig_idx is None:
        return False

    insert_idx = _annotation_block_start(lines, sig_idx, lang)
    # Look for existing docs above the annotation block (or signature)
    look_from = max(0, insert_idx - 8)
    window = "".join(lines[look_from : sig_idx + 4])
    has_doc = '"""' in window or "/**" in window or (
        lang == "go" and bool(re.search(r"(?m)^[ \t]*//", window))
    )
    if has_doc and not replace:
        return False
    if has_doc and replace:
        insert_idx = _remove_leading_doc(lines, insert_idx, lang)
        # Recompute after removal: signature may have shifted
        sig_idx = _find_signature_line(lines, simple_name, patterns)
        if sig_idx is None:
            return False
        insert_idx = _annotation_block_start(lines, sig_idx, lang)

    indent = ""
    if sig_idx + 1 < len(lines):
        raw = lines[sig_idx + 1]
        indent = raw[: len(raw) - len(raw.lstrip())]
    prefix_indent = lines[sig_idx][: len(lines[sig_idx]) - len(lines[sig_idx].lstrip())]

    insertion = wrapped if wrapped.endswith("\n") else wrapped + "\n"
    if "{INDENT}" in insertion:
        insertion = insertion.replace("{INDENT}", indent or "    ")
        lines.insert(sig_idx + 1, insertion)
    else:
        insertion = insertion.replace("{PREFIX}", prefix_indent)
        lines.insert(insert_idx, insertion)

    path.write_text("".join(lines), encoding="utf-8")
    return True


def write_unit_doc(workspace: Path, gap: DocumentGap, unit) -> str | None:
    """Write pack-wrapped unit.description into source (or marker fallback)."""
    pack = get_pack(gap.language)
    if pack is None:
        return None
    path = resolve_repo_file(workspace, gap.repo, gap.path)
    simple = unit.qualified_name.rsplit(".", 1)[-1]
    lang = (gap.language or "").lower()
    inner = (unit.description or "").strip()
    if not inner:
        inner = ensure_marker(f"TODO: characterize {unit.qualified_name}.")
    wrapped = pack.wrap(inner)
    replace = is_replaceable_doc(unit)
    ok = _insert_doc_at_signature(
        path,
        simple,
        wrapped,
        patterns=_patterns_for(lang),
        lang=lang,
        replace=replace,
    )
    return f"{gap.repo}/{gap.path}" if ok else None


# Back-compat name used by analyze / document stage
def write_doc_stub(workspace: Path, gap: DocumentGap, unit) -> str | None:
    return write_unit_doc(workspace, gap, unit)


def run_document(config: AutorefactorConfig) -> DocumentReport:
    """Detect missing/replaceable docs and insert pack-wrapped descriptions."""
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
        "# autorefactor document gaps\n\n"
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
        path = write_unit_doc(workspace, gap, unit)
        if path:
            touched.append(path)

    report = DocumentReport(
        summary=(
            f"Document stage: {len(gaps)} gap(s); wrote {len(touched)} doc(s). "
            f"Owned docs include marker `{AUTOREFACTOR_DOC_MARKER}`."
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
