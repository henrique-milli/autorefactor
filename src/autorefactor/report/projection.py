"""Persist 3D embedding projection + graph edges for the interactive report UI."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

from autorefactor.changesets.models import Changeset
from autorefactor.models import CodeUnit
from autorefactor.report.roi import upside_kind

_DEDUP_ACTIONS = frozenset({"merge", "extract_shared", "delete_duplicate"})
_MIN_POINTS = 5


def _unit_changeset_index(
    changesets: list[Changeset],
) -> tuple[dict[str, list[str]], dict[str, str]]:
    """Map unit_id -> changeset ids; unit_id -> primary upside_kind."""
    by_unit: dict[str, list[str]] = {}
    kind_by_unit: dict[str, str] = {}
    for cs in changesets:
        kind = upside_kind(cs)
        for t in cs.targets:
            uid = t.unit_id
            if not uid:
                continue
            by_unit.setdefault(uid, []).append(cs.id)
            # Prefer dedup kind when multiple
            prev = kind_by_unit.get(uid)
            if prev is None or (kind == "dedup" and prev != "dedup"):
                kind_by_unit[uid] = kind
    return by_unit, kind_by_unit


def compute_3d_coords(
    vectors: list[list[float]] | np.ndarray,
) -> tuple[np.ndarray, str] | None:
    """Project high-dim embeddings to 3D (PCA preferred; t-SNE fallback).

    Returns ``(coords, method)`` where method is ``pca3`` or ``tsne3``.
    """
    matrix = np.asarray(vectors, dtype=np.float64)
    if matrix.ndim != 2 or matrix.shape[0] < _MIN_POINTS:
        return None
    n = matrix.shape[0]
    # Prefer PCA for speed/stability in interactive maps
    try:
        from sklearn.decomposition import PCA

        n_comp = min(3, matrix.shape[1], n - 1)
        if n_comp < 2:
            return None
        coords = PCA(n_components=n_comp, random_state=42).fit_transform(matrix)
        if coords.shape[1] == 2:
            z = np.zeros((n, 1), dtype=np.float64)
            coords = np.hstack([coords, z])
        elif coords.shape[1] == 1:
            pad = np.zeros((n, 2), dtype=np.float64)
            coords = np.hstack([coords, pad])
        return coords.astype(np.float64), "pca3"
    except Exception:  # noqa: BLE001
        pass

    from sklearn.manifold import TSNE

    perplexity = max(2, min(30, n - 1))
    coords = TSNE(
        n_components=3,
        perplexity=perplexity,
        init="random",
        learning_rate="auto",
        random_state=42,
    ).fit_transform(matrix)
    return coords.astype(np.float64), "tsne3"


def build_projection_payload(
    units: list[CodeUnit],
    changesets: list[Changeset],
) -> dict[str, Any] | None:
    vectors: list[list[float]] = []
    meta: list[CodeUnit] = []
    for unit in units:
        if not unit.embedding:
            continue
        if unit.kind.value not in {"function", "type"}:
            continue
        vectors.append(list(unit.embedding))
        meta.append(unit)
    if len(vectors) < _MIN_POINTS:
        return None

    result = compute_3d_coords(vectors)
    if result is None:
        return None
    coords, method = result

    by_unit, kind_by_unit = _unit_changeset_index(changesets)
    points: list[dict[str, Any]] = []
    for i, unit in enumerate(meta):
        cs_ids = by_unit.get(unit.id) or []
        points.append(
            {
                "unit_id": unit.id,
                "repo": unit.repo,
                "path": unit.path,
                "qualified_name": unit.qualified_name,
                "kind": unit.kind.value,
                "x": float(coords[i, 0]),
                "y": float(coords[i, 1]),
                "z": float(coords[i, 2]),
                "on_queue": bool(cs_ids),
                "changeset_ids": cs_ids,
                "upside_kind": kind_by_unit.get(unit.id),
            }
        )
    return {
        "method": method,
        "random_state": 42,
        "point_count": len(points),
        "points": points,
    }


def build_graph_payload(
    changesets: list[Changeset],
    *,
    agenda: object | None = None,
) -> dict[str, Any]:
    edges: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    for cs in changesets:
        ids = [t.unit_id for t in cs.targets if t.unit_id]
        if len(ids) >= 2:
            edge_kind = "clone" if cs.action in _DEDUP_ACTIONS else "cluster"
            for a, b in zip(ids, ids[1:]):
                key = (a, b, cs.id)
                if key in seen:
                    continue
                seen.add(key)
                edges.append(
                    {
                        "source": a,
                        "target": b,
                        "changeset_id": cs.id,
                        "action": cs.action,
                        "kind": edge_kind,
                    }
                )
            if len(ids) > 2:
                key = (ids[0], ids[-1], cs.id)
                if key not in seen:
                    seen.add(key)
                    edges.append(
                        {
                            "source": ids[0],
                            "target": ids[-1],
                            "changeset_id": cs.id,
                            "action": cs.action,
                            "kind": edge_kind,
                        }
                    )
        # depends_on: link first target of dependent -> first target of dependency
        for dep_id in cs.depends_on or []:
            dep = next((c for c in changesets if c.id == dep_id), None)
            if dep is None:
                continue
            src = next((t.unit_id for t in cs.targets if t.unit_id), None)
            tgt = next((t.unit_id for t in dep.targets if t.unit_id), None)
            if not src or not tgt:
                continue
            key = (src, tgt, f"dep:{cs.id}:{dep_id}")
            if key in seen:
                continue
            seen.add(key)
            edges.append(
                {
                    "source": src,
                    "target": tgt,
                    "changeset_id": cs.id,
                    "action": cs.action,
                    "kind": "depends_on",
                }
            )

    if agenda is not None and hasattr(agenda, "entries"):
        # Light cluster edges: multi-flag agenda units already covered by queue;
        # skip dense all-pairs — only link entries that share a component id if present
        components = getattr(agenda, "components", None)
        if components and isinstance(components, list):
            for comp in components:
                uids = list(getattr(comp, "unit_ids", None) or [])
                if len(uids) < 2:
                    continue
                for a, b in zip(uids[:8], uids[1:9]):
                    key = (a, b, f"cluster:{a}:{b}")
                    if key in seen:
                        continue
                    seen.add(key)
                    edges.append(
                        {
                            "source": a,
                            "target": b,
                            "changeset_id": "",
                            "action": "cluster",
                            "kind": "cluster",
                        }
                    )

    return {"edge_count": len(edges), "edges": edges}


def write_projection_artifacts(
    report_dir: Path,
    units: list[CodeUnit],
    changesets: list[Changeset],
    *,
    agenda: object | None = None,
) -> dict[str, str | None]:
    """Write ``projection.json`` and ``graph.json`` under ``report_dir``."""
    report_dir.mkdir(parents=True, exist_ok=True)
    projection = build_projection_payload(units, changesets)
    graph = build_graph_payload(changesets, agenda=agenda)

    proj_path = report_dir / "projection.json"
    graph_path = report_dir / "graph.json"
    out_proj: str | None = None
    if projection is not None:
        proj_path.write_text(json.dumps(projection, indent=2), encoding="utf-8")
        out_proj = str(proj_path)
    graph_path.write_text(json.dumps(graph, indent=2), encoding="utf-8")
    return {"projection": out_proj, "graph": str(graph_path)}
