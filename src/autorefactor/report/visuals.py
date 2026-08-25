from __future__ import annotations

from pathlib import Path

import numpy as np

from autorefactor.changesets.models import Changeset
from autorefactor.index.hierarchy import simple_name
from autorefactor.models import CodeUnit
from autorefactor.report.roi import enrich_changeset_rows

_KIND_COLORS = {
    "function": "#4C78A8",
    "type": "#F58518",
    "file": "#54A24B",
    "module": "#B279A2",
}


def write_embedding_projection(
    path: Path,
    units: list[CodeUnit],
    changesets: list[Changeset],
    agenda: object | None = None,
) -> str | None:
    vectors = []
    meta: list[CodeUnit] = []
    for unit in units:
        if not unit.embedding:
            continue
        if unit.kind.value not in {"function", "type"}:
            continue
        vectors.append(unit.embedding)
        meta.append(unit)
    if len(vectors) < 5:
        return None

    highlight_ids = {
        t.unit_id
        for cs in changesets
        for t in cs.targets
        if t.unit_id
    }

    # t-SNE is roughly O(n²); subsample large maps so multi-repo analyze finishes.
    _MAX_POINTS = 2500
    if len(vectors) > _MAX_POINTS:
        rng = np.random.default_rng(42)
        prefer = [i for i, u in enumerate(meta) if u.id in highlight_ids]
        rest = [i for i in range(len(meta)) if i not in set(prefer)]
        need = max(0, _MAX_POINTS - len(prefer))
        if need and rest:
            pick_rest = rng.choice(rest, size=min(need, len(rest)), replace=False).tolist()
        else:
            pick_rest = []
        keep = sorted((prefer + pick_rest)[:_MAX_POINTS])
        vectors = [vectors[i] for i in keep]
        meta = [meta[i] for i in keep]

    from sklearn.manifold import TSNE

    matrix = np.array(vectors, dtype=np.float32)
    perplexity = max(2, min(30, len(vectors) - 1))
    coords = TSNE(
        n_components=2,
        perplexity=perplexity,
        init="random",
        learning_rate="auto",
        random_state=42,
    ).fit_transform(matrix)

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D

    id_to_idx = {u.id: i for i, u in enumerate(meta)}

    path.parent.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(9, 7))

    # Color by unit kind
    for kind, color in _KIND_COLORS.items():
        mask = [
            i
            for i, u in enumerate(meta)
            if u.kind.value == kind and u.id not in highlight_ids
        ]
        if not mask:
            continue
        ax.scatter(
            coords[mask, 0],
            coords[mask, 1],
            s=18,
            color=color,
            label=kind,
            alpha=0.5,
            zorder=2,
        )

    # Clone-pair edges: changesets with ≥2 resolved unit_ids
    edge_drawn = False
    for cs in changesets:
        ids = [t.unit_id for t in cs.targets if t.unit_id and t.unit_id in id_to_idx]
        if len(ids) < 2:
            continue
        pts = [id_to_idx[i] for i in ids]
        for a, b in zip(pts, pts[1:]):
            ax.plot(
                [coords[a, 0], coords[b, 0]],
                [coords[a, 1], coords[b, 1]],
                color="#C44E52",
                alpha=0.55,
                linewidth=1.2,
                zorder=3,
            )
            edge_drawn = True
        if len(pts) > 2:
            a, b = pts[0], pts[-1]
            ax.plot(
                [coords[a, 0], coords[b, 0]],
                [coords[a, 1], coords[b, 1]],
                color="#C44E52",
                alpha=0.35,
                linewidth=1.0,
                linestyle="--",
                zorder=3,
            )
            edge_drawn = True

    # Star highlight queue targets
    hi = [i for i, u in enumerate(meta) if u.id in highlight_ids]
    if hi:
        ax.scatter(
            coords[hi, 0],
            coords[hi, 1],
            s=160,
            marker="*",
            c="#C44E52",
            edgecolors="#5A1A1C",
            linewidths=0.6,
            label="queue target",
            zorder=5,
        )
        for i in hi[:12]:
            ax.annotate(
                simple_name(meta[i].qualified_name)[:18],
                (coords[i, 0], coords[i, 1]),
                fontsize=7,
                alpha=0.9,
            )

    agenda_multi: set[str] = set()
    if agenda is not None and hasattr(agenda, "entries"):
        for entry in agenda.entries:
            if len(getattr(entry, "flag_ids", []) or []) >= 2:
                agenda_multi.add(entry.unit_id)
    multi = [i for i, u in enumerate(meta) if u.id in agenda_multi]
    if multi:
        ax.scatter(
            coords[multi, 0],
            coords[multi, 1],
            s=120,
            facecolors="none",
            edgecolors="#E45756",
            linewidths=1.5,
            linestyle="--",
            label="multi-flag agenda",
            zorder=4,
        )

    ax.set_title(
        "Units (function/type) — t-SNE; ★ = queue; red lines = clone-pair edges"
    )
    handles, labels = ax.get_legend_handles_labels()
    if edge_drawn:
        handles.append(Line2D([0], [0], color="#C44E52", lw=1.2, label="clone edge"))
        labels.append("clone edge")
    ax.legend(handles, labels, markerscale=1.2, fontsize=7, loc="best")
    fig.tight_layout()
    fig.savefig(path, dpi=140)
    plt.close(fig)
    return str(path)


def write_debt_components(
    path: Path,
    components: dict[str, float] | None,
) -> str | None:
    if not components:
        return None
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    order = ["complexity", "clone", "coverage", "docs", "slop"]
    labels = [k for k in order if k in components] or list(components.keys())
    values = [float(components.get(k) or 0) for k in labels]
    if not any(v > 0 for v in values) and not labels:
        return None

    path.parent.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(8, max(2.5, 0.45 * len(labels) + 1)))
    ax.barh(labels[::-1], values[::-1], color="#E45756")
    ax.set_xlim(0, 100)
    ax.set_xlabel("Score (estimate, 0–100)")
    ax.set_title("Technical debt components (estimate)")
    fig.tight_layout()
    fig.savefig(path, dpi=140)
    plt.close(fig)
    return str(path)


def write_upside_bars(
    path: Path,
    changesets: list[Changeset],
    *,
    ranked_rows: list[dict] | None = None,
) -> str | None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    if ranked_rows is None:
        ranked_rows = enrich_changeset_rows(
            [
                {
                    "id": cs.id,
                    "title": cs.title,
                    "action": cs.action,
                    "estimated_loc_removed": int(cs.metrics_delta.get("estimated_loc_removed") or 0),
                    "loc_under_review": int(cs.metrics_delta.get("loc_under_review") or 0),
                    "confidence": cs.confidence,
                }
                for cs in changesets
                if cs.targets
            ],
            changesets,
        )

    rows = [r for r in ranked_rows if float(r.get("value_score") or 0) > 0 or r.get("action")]
    if not rows:
        return None

    path.parent.mkdir(parents=True, exist_ok=True)
    top = rows[:15]
    labels = []
    values = []
    colors = []
    kind_color = {
        "dedup": "#4C78A8",
        "structure": "#F58518",
        "safety": "#54A24B",
        "docs": "#B279A2",
    }
    for r in top:
        kind = str(r.get("upside_kind") or "structure")
        title = (r.get("title") or r.get("id") or "?")[:36]
        labels.append(f"{title} [{kind}]")
        values.append(float(r.get("value_score") or 0))
        colors.append(kind_color.get(kind, "#4C78A8"))

    fig, ax = plt.subplots(figsize=(9, max(3, 0.4 * len(labels) + 1)))
    ax.barh(labels[::-1], values[::-1], color=colors[::-1])
    ax.set_xlabel("value_score (estimate)")
    ax.set_xlim(0, 1.05)
    ax.set_title("Estimated value (not just confidence)")
    fig.tight_layout()
    fig.savefig(path, dpi=140)
    plt.close(fig)
    return str(path)


def write_before_after_bars(
    path: Path,
    changesets: list[Changeset],
) -> str | None:
    from collections import Counter

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    if not changesets:
        return None
    counts = Counter(cs.action for cs in changesets)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(7, 4))
    actions = list(counts.keys())
    values = [counts[a] for a in actions]
    ax.bar(actions, values, color="#4C78A8")
    ax.set_title("Pending changesets by action")
    ax.set_ylabel("count")
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)
    return str(path)


def write_goal_bars(path: Path, changesets: list[Changeset]) -> str | None:
    from collections import Counter

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    if not changesets:
        return None
    counts = Counter(str(cs.metrics_delta.get("goal") or "unknown") for cs in changesets)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(7, 4))
    goals = list(counts.keys())
    values = [counts[g] for g in goals]
    ax.bar(goals, values, color="#72B7B2")
    ax.set_title("Pending changesets by analysis goal")
    ax.set_ylabel("count")
    ax.tick_params(axis="x", rotation=30)
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)
    return str(path)


def generate_visuals(
    visuals_dir: Path,
    units: list[CodeUnit],
    changesets: list[Changeset],
    agenda: object | None = None,
    metrics: dict | None = None,
    *,
    report_dir: Path | None = None,
) -> list[str]:
    visuals_dir.mkdir(parents=True, exist_ok=True)
    paths: list[str] = []
    emb = write_embedding_projection(
        visuals_dir / "embeddings_tsne.png", units, changesets, agenda=agenda
    )
    if emb:
        paths.append(emb)

    components = None
    ranked_rows = None
    if metrics:
        components = ((metrics.get("roi") or {}).get("debt_score") or {}).get("components")
        ranked_rows = metrics.get("upside_by_changeset")

    debt = write_debt_components(visuals_dir / "debt_components.png", components)
    if debt:
        paths.append(debt)

    upside = write_upside_bars(
        visuals_dir / "upside_by_changeset.png",
        changesets,
        ranked_rows=ranked_rows,
    )
    if upside:
        paths.append(upside)

    bars = write_before_after_bars(visuals_dir / "changesets_by_action.png", changesets)
    if bars:
        paths.append(bars)
    goal_bars = write_goal_bars(visuals_dir / "changesets_by_goal.png", changesets)
    if goal_bars:
        paths.append(goal_bars)

    # Interactive map payload (JSON next to visuals/)
    dest = report_dir if report_dir is not None else visuals_dir.parent
    from autorefactor.report.projection import write_projection_artifacts

    write_projection_artifacts(dest, units, changesets, agenda=agenda)
    return paths
