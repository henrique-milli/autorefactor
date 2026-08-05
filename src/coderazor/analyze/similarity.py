from __future__ import annotations

import difflib
from collections import defaultdict

import numpy as np

from coderazor.index.hierarchy import simple_name
from coderazor.models import (
    CodeUnit,
    Flag,
    FlagSource,
    Severity,
    SuggestedAction,
    UnitKind,
    content_hash,
)

PAIR_KINDS = {
    UnitKind.FUNCTION,
    UnitKind.TYPE,
    UnitKind.PROTO_MESSAGE,
    UnitKind.PROTO_RPC,
    UnitKind.FILE,
    UnitKind.MODULE,
}


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denom == 0:
        return 0.0
    return float(np.dot(a, b) / denom)


def find_similarity_flags(
    units: list[CodeUnit],
    *,
    threshold: float = 0.78,
    max_pairs_per_kind: int = 500,
) -> list[Flag]:
    by_kind: dict[UnitKind, list[CodeUnit]] = defaultdict(list)
    for unit in units:
        if unit.kind in PAIR_KINDS and unit.embedding:
            by_kind[unit.kind].append(unit)

    flags: list[Flag] = []
    for kind, group in by_kind.items():
        if len(group) < 2:
            continue
        matrix = np.array([u.embedding for u in group], dtype=np.float64)
        scored: list[tuple[float, int, int]] = []
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                sim = _cosine(matrix[i], matrix[j])
                if sim < threshold:
                    continue
                scored.append((sim, i, j))
        scored.sort(reverse=True)
        for sim, i, j in scored[:max_pairs_per_kind]:
            a, b = group[i], group[j]
            name_sim = difflib.SequenceMatcher(
                None,
                simple_name(a.qualified_name).lower(),
                simple_name(b.qualified_name).lower(),
            ).ratio()
            source_sim = difflib.SequenceMatcher(
                None,
                " ".join(a.source_snippet.split()),
                " ".join(b.source_snippet.split()),
            ).ratio()
            cross = a.repo != b.repo
            if source_sim >= 0.85:
                action = SuggestedAction.DELETE_DUPLICATE
                title = f"Near-copy {kind.value}: {a.qualified_name} ≈ {b.qualified_name}"
            elif cross:
                action = SuggestedAction.EXTRACT_SHARED
                title = f"Cross-repo similar {kind.value}: {a.qualified_name} ≈ {b.qualified_name}"
            else:
                action = SuggestedAction.MOVE
                title = f"Similar {kind.value} should be colocated: {a.qualified_name} ≈ {b.qualified_name}"

            severity = Severity.HIGH if sim >= 0.9 or source_sim >= 0.85 else Severity.MEDIUM
            flags.append(
                Flag(
                    id=content_hash(f"sim:{a.id}:{b.id}"),
                    source=FlagSource.SIMILARITY,
                    severity=severity,
                    title=title,
                    evidence=(
                        f"cosine={sim:.3f} name={name_sim:.3f} source={source_sim:.3f} "
                        f"repos={a.repo},{b.repo}"
                    ),
                    unit_ids=[a.id, b.id],
                    suggested_action=action,
                    score=sim,
                    metadata={
                        "cosine": sim,
                        "name_similarity": name_sim,
                        "source_similarity": source_sim,
                        "cross_repo": cross,
                        "kind": kind.value,
                    },
                )
            )
    return flags
