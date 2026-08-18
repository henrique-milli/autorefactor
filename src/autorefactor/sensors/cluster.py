from __future__ import annotations

import logging

import numpy as np

from autorefactor.domain import (
    AnalysisGoal,
    Finding,
    Severity,
    SuggestedAction,
    UnitKind,
    content_hash,
    try_make_finding,
)
from autorefactor.sensors.base import AnalysisContext

logger = logging.getLogger(__name__)


class ClusterSensor:
    id = "cluster"
    goal = AnalysisGoal.CLUSTER

    def available(self, ctx: AnalysisContext) -> bool:
        return sum(1 for u in ctx.units if u.embedding and u.kind == UnitKind.FUNCTION) >= 6

    def collect(self, ctx: AnalysisContext) -> list[Finding]:
        candidates = [u for u in ctx.units if u.kind == UnitKind.FUNCTION and u.embedding]
        if len(candidates) < 6:
            return []
        try:
            import hdbscan
        except ImportError:
            logger.warning("hdbscan not available; skipping cluster sensor")
            return []
        matrix = np.array([u.embedding for u in candidates], dtype=np.float64)
        labels = hdbscan.HDBSCAN(min_cluster_size=3, metric="euclidean").fit_predict(matrix)
        by_label: dict[int, list] = {}
        for unit, label in zip(candidates, labels, strict=True):
            if label < 0:
                continue
            by_label.setdefault(int(label), []).append(unit)
        known = ctx.known_ids()
        findings: list[Finding] = []
        for label, members in by_label.items():
            repos = {m.repo for m in members}
            if len(repos) < 2:
                continue
            f = try_make_finding(
                id=content_hash(f"cluster:{label}:{','.join(sorted(m.id for m in members))}"),
                goal=self.goal,
                sensor=self.id,
                severity=Severity.MEDIUM,
                title=f"Multi-repo semantic cluster #{label}",
                evidence=f"{len(members)} functions across {', '.join(sorted(repos))}",
                unit_ids=[m.id for m in members],
                action_hint=SuggestedAction.EXTRACT_SHARED,
                score=0.65,
                metrics={"size": float(len(members))},
                metadata={"cluster_id": label, "repos": sorted(repos)},
                known_ids=known,
            )
            if f:
                findings.append(f)
        return findings
