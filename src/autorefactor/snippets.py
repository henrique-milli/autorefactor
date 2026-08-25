"""Shared source snippet helpers and changeset target enrichment."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from autorefactor.config import MAX_SOURCE_CHARS


def truncate_source_snippet(text: str | None) -> str:
    """Return the full snippet for review surfaces.

    Extract already caps stored units at ``MAX_SOURCE_CHARS``; this only
    re-applies that ceiling as a safety bound (no tighter clip).
    """
    raw = text or ""
    if not raw:
        return ""
    if len(raw) > MAX_SOURCE_CHARS:
        return raw[:MAX_SOURCE_CHARS]
    return raw


def _target_fields(target: Any) -> dict[str, Any]:
    if isinstance(target, dict):
        return {
            "repo": target.get("repo") or "",
            "path": target.get("path") or "",
            "symbol": target.get("symbol"),
            "unit_id": target.get("unit_id"),
        }
    return {
        "repo": getattr(target, "repo", "") or "",
        "path": getattr(target, "path", "") or "",
        "symbol": getattr(target, "symbol", None),
        "unit_id": getattr(target, "unit_id", None),
    }


def _resolve_unit_for_target(target: Any, units: list[Any]) -> Any | None:
    fields = _target_fields(target)
    by_id = {u.id: u for u in units}
    unit = by_id.get(fields["unit_id"]) if fields["unit_id"] else None
    if unit is not None:
        return unit
    symbol = fields["symbol"]
    for u in units:
        if u.repo != fields["repo"] or u.path != fields["path"]:
            continue
        if not symbol or symbol in (u.qualified_name or ""):
            return u
    return None


def enrich_targets(out_dir: Path, targets: list[Any] | None) -> list[dict[str, Any]]:
    """Resolve targets against index units and attach full source snippets."""
    if not targets:
        return []
    from autorefactor.artifacts import layout_for, load_units

    try:
        units = load_units(layout_for(out_dir))
    except Exception:  # noqa: BLE001
        units = []
    out: list[dict[str, Any]] = []
    for target in targets:
        fields = _target_fields(target)
        row: dict[str, Any] = dict(fields)
        unit = _resolve_unit_for_target(target, units)
        if unit is not None:
            row["qualified_name"] = unit.qualified_name
            snippet = truncate_source_snippet(getattr(unit, "source_snippet", None))
            if snippet:
                row["source_snippet"] = snippet
        out.append(row)
    return out


def enrich_targets_from_snippet_map(
    targets: list[Any] | None,
    snippet_by_id: dict[str, str],
) -> list[dict[str, Any]]:
    """Join pre-loaded source snippets onto target dicts (HTTP UI path)."""
    if not targets:
        return []
    out: list[dict[str, Any]] = []
    for target in targets:
        if not isinstance(target, dict):
            fields = _target_fields(target)
            row = dict(fields)
        else:
            row = dict(target)
        uid = row.get("unit_id")
        snippet = snippet_by_id.get(str(uid)) if uid else None
        if snippet and "source_snippet" not in row:
            row["source_snippet"] = snippet
        out.append(row)
    return out


def snippet_blocks_for_digest(enriched_targets: list[dict[str, Any]], *, max_targets: int = 8) -> str:
    """Markdown fenced code blocks for up to ``max_targets`` snippets."""
    blocks: list[str] = []
    for target in enriched_targets[:max_targets]:
        snippet = (target.get("source_snippet") or "").strip()
        if not snippet:
            continue
        repo = target.get("repo") or ""
        path = target.get("path") or ""
        symbol = target.get("symbol") or target.get("qualified_name") or ""
        label = "/".join(p for p in (repo, path) if p)
        if symbol:
            label = f"{label} :: {symbol}" if label else str(symbol)
        header = f"\n\n**{label}**" if label else ""
        ext = Path(str(path)).suffix.lstrip(".")
        lang = ext if ext and len(ext) <= 12 else ""
        blocks.append(f"{header}\n```{lang}\n{snippet}\n```")
    return "".join(blocks)
