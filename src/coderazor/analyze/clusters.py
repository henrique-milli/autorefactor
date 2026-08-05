from __future__ import annotations

import logging

import numpy as np

from coderazor.models import (
    CodeUnit,
    Flag,
    FlagSource,
    Severity,
    SuggestedAction,
    UnitKind,
    content_hash,
)

logger = logging.getLogger(__name__)


def find_cluster_flags(units: list[CodeUnit], *, min_cluster_size: int = 3) -> list[Flag]:
    """Optional HDBSCAN clustering on function embeddings spanning multiple repos."""
    candidates = [
        u
        for u in units
        if u.kind == UnitKind.FUNCTION and u.embedding
    ]
    if len(candidates) < min_cluster_size * 2:
        return []

    try:
        import hdbscan
    except ImportError:
        logger.warning("hdbscan not available; skipping cluster flags")
        return []

    matrix = np.array([u.embedding for u in candidates], dtype=np.float64)
    clusterer = hdbscan.HDBSCAN(min_cluster_size=min_cluster_size, metric="euclidean")
    labels = clusterer.fit_predict(matrix)

    by_label: dict[int, list[CodeUnit]] = {}
    for unit, label in zip(candidates, labels, strict=True):
        if label < 0:
            continue
        by_label.setdefault(int(label), []).append(unit)

    flags: list[Flag] = []
    for label, members in by_label.items():
        repos = {m.repo for m in members}
        if len(repos) < 2:
            continue
        flags.append(
            Flag(
                id=content_hash(f"cluster:{label}:{','.join(sorted(m.id for m in members))}"),
                source=FlagSource.CLUSTER,
                severity=Severity.MEDIUM,
                title=f"Multi-repo semantic cluster #{label}",
                evidence=f"{len(members)} functions across {', '.join(sorted(repos))}",
                unit_ids=[m.id for m in members],
                suggested_action=SuggestedAction.EXTRACT_SHARED,
                score=0.65,
                metadata={"cluster_id": label, "repos": sorted(repos), "size": len(members)},
            )
        )
    return flags
