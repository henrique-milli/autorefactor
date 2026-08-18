"""Attach language-neutral documentation / signature / body facets to units."""

from __future__ import annotations

from pathlib import Path

from autorefactor.document.packs import get_pack
from autorefactor.domain import CodeUnit, UnitKind
from autorefactor.paths import resolve_repo_file

# Bytes before AST start to search for leading docs (Javadoc, etc.)
_LEADING_DOC_WINDOW = 800

_LEAF_KINDS = {
    UnitKind.FUNCTION,
    UnitKind.TYPE,
    UnitKind.PROTO_MESSAGE,
    UnitKind.PROTO_RPC,
    UnitKind.PROTO_SERVICE,
}


def _strip_leading_doc(snippet: str, documentation: str | None) -> str:
    """Best-effort remove a leading doc block already extracted as documentation."""
    text = snippet or ""
    if not documentation or not text:
        return text
    stripped = text.lstrip()
    # If snippet starts with a block/docstring, drop until after first closing delimiter.
    if stripped.startswith("/**"):
        end = stripped.find("*/")
        if end >= 0:
            return stripped[end + 2 :].lstrip("\n")
    if stripped.startswith('"""') or stripped.startswith("'''"):
        q = stripped[:3]
        end = stripped.find(q, 3)
        if end >= 0:
            return stripped[end + 3 :].lstrip("\n")
    if stripped.startswith("/*"):
        end = stripped.find("*/")
        if end >= 0:
            return stripped[end + 2 :].lstrip("\n")
    return text


def _signature_from_body(body: str) -> str | None:
    for line in (body or "").splitlines():
        s = line.strip()
        if s:
            return s[:240]
    return None


def _leading_window(source: bytes, start_byte: int) -> str:
    lo = max(0, int(start_byte) - _LEADING_DOC_WINDOW)
    return source[lo : max(lo, int(start_byte))].decode("utf-8", errors="replace")


def attach_text_facets(workspace: Path, units: list[CodeUnit]) -> list[CodeUnit]:
    """Populate documentation / signature / body using DocPacks (syntax-hidden)."""
    root = workspace.resolve()
    file_cache: dict[tuple[str, str], bytes | None] = {}
    out: list[CodeUnit] = []

    for unit in units:
        if unit.kind not in _LEAF_KINDS:
            out.append(unit)
            continue

        pack = get_pack(unit.language)
        snippet = unit.source_snippet or ""
        documentation: str | None = unit.documentation
        body: str | None = unit.body
        signature: str | None = unit.signature

        raw_bytes: bytes | None = None
        key = (unit.repo, unit.path)
        if key not in file_cache:
            try:
                path = resolve_repo_file(root, unit.repo, unit.path)
                file_cache[key] = path.read_bytes() if path.is_file() else None
            except (OSError, ValueError):
                file_cache[key] = None
        raw_bytes = file_cache[key]

        window = snippet
        if raw_bytes is not None and unit.span and unit.span.start_byte >= 0:
            lead = _leading_window(raw_bytes, unit.span.start_byte)
            window = f"{lead}{snippet}"

        if pack is not None and not documentation:
            documentation = pack.extract(window) or pack.extract(snippet)

        if body is None:
            body = _strip_leading_doc(snippet, documentation) if documentation else snippet
        if signature is None:
            signature = _signature_from_body(body or snippet)

        out.append(
            unit.model_copy(
                update={
                    "documentation": documentation,
                    "signature": signature,
                    "body": body,
                }
            )
        )
    return out
