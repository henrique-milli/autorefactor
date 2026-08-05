from __future__ import annotations

from pathlib import Path

import numpy as np

from coderazor.changesets.models import Changeset
from coderazor.models import CodeUnit


def write_embedding_projection(
    path: Path,
    units: list[CodeUnit],
    changesets: list[Changeset],
) -> str | None:
    vectors = []
    labels = []
    for unit in units:
        if not unit.embedding:
            continue
        vectors.append(unit.embedding)
        labels.append(unit.repo)
    if len(vectors) < 5:
        return None

    from sklearn.manifold import TSNE

    matrix = np.array(vectors, dtype=np.float64)
    # t-SNE needs perplexity < n_samples
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

    highlight = {
        (t.repo, t.path)
        for cs in changesets
        for t in cs.targets
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(8, 6))
    repos = sorted(set(labels))
    cmap = plt.get_cmap("tab10")
    for idx, repo in enumerate(repos):
        mask = [i for i, lab in enumerate(labels) if lab == repo]
        ax.scatter(
            coords[mask, 0],
            coords[mask, 1],
            s=12,
            color=cmap(idx % 10),
            label=repo,
            alpha=0.7,
        )
    # Highlight changeset targets roughly by repo match (path-level exact needs unit list)
    ax.set_title("Code unit embedding projection (t-SNE)")
    ax.legend(markerscale=2, fontsize=7, loc="best")
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)
    _ = highlight  # reserved for richer highlight later
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
    ax.set_title("Pending changesets by action (before host-agent apply)")
    ax.set_ylabel("count")
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)
    return str(path)


def generate_visuals(
    visuals_dir: Path,
    units: list[CodeUnit],
    changesets: list[Changeset],
) -> list[str]:
    visuals_dir.mkdir(parents=True, exist_ok=True)
    paths: list[str] = []
    emb = write_embedding_projection(visuals_dir / "embeddings_tsne.png", units, changesets)
    if emb:
        paths.append(emb)
    bars = write_before_after_bars(visuals_dir / "changesets_by_action.png", changesets)
    if bars:
        paths.append(bars)
    return paths
