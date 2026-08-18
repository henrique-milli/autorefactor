"""Structural fingerprint clone sensor — deterministic anti-slop layer."""

from __future__ import annotations

from autorefactor.domain import (
    AnalysisGoal,
    Finding,
    Severity,
    SuggestedAction,
    UnitKind,
    content_hash,
    try_make_finding,
)
from autorefactor.fingerprint import candidate_pairs, fingerprints_for_source
from autorefactor.sensors.base import AnalysisContext, Sensor
from autorefactor.sensors.similarity import (
    _DOMAIN_DIVERGENCE_SKIP,
    _is_constructor,
    _is_dto_path,
    _loc,
    domain_token_divergence,
    is_test_path,
    return_semantics_differ,
    simple_name,
    LOW_SIGNAL_NAMES,
)


def _skip_trivial(unit) -> bool:  # noqa: ANN001
    name = simple_name(unit.qualified_name)
    if name.lower() in LOW_SIGNAL_NAMES:
        return True
    if _is_constructor(unit):
        return True
    return False


class FingerprintCloneSensor:
    """MOSS-style winnowing clones; works offline without embeddings."""

    id = "sim.fingerprint"
    goal = AnalysisGoal.STRUCTURAL_CLONE

    def available(self, ctx: AnalysisContext) -> bool:
        return any(u.kind == UnitKind.FUNCTION for u in ctx.units)

    def collect(self, ctx: AnalysisContext) -> list[Finding]:
        threshold = float(getattr(ctx.config, "fingerprint_threshold", None) or 0.72)
        max_pairs = int(getattr(ctx.config, "max_fingerprint_pairs", None) or 80)
        # cross_repo/slop: prioritize multi-repo AI reimplementation; same-repo
        # same-name pairs (mapResponse×N handlers) are framework patterns, not slop.
        prefer_cross = ctx.focus in {"cross_repo", "slop"}
        include_tests = ctx.focus == "strict"

        units = []
        for u in ctx.units_list():
            if u.kind != UnitKind.FUNCTION:
                continue
            if _skip_trivial(u):
                continue
            if not include_tests and is_test_path(u.path):
                continue
            if _is_dto_path(u.path):
                continue
            if _loc(u) < 8:
                continue
            units.append(u)

        fps: dict[str, set[int]] = {}
        by_id = {u.id: u for u in units}
        for u in units:
            fp = fingerprints_for_source(u.source_snippet)
            if len(fp) >= 3:
                fps[u.id] = fp

        known = ctx.known_ids()
        scored: list[tuple[float, Finding]] = []
        for a_id, b_id, score in candidate_pairs(fps, min_shared=2):
            if score < threshold:
                continue
            a, b = by_id[a_id], by_id[b_id]
            if a.qualified_name == b.qualified_name and a.repo == b.repo:
                continue
            cross = a.repo != b.repo
            if not cross and return_semantics_differ(a.source_snippet or "", b.source_snippet or ""):
                continue
            if a.path == b.path:
                continue
            name_a = simple_name(a.qualified_name)
            name_b = simple_name(b.qualified_name)
            # Same-repo + identical method name ≈ intentional local pattern
            if not cross and name_a == name_b:
                continue
            # Same-repo renames need a higher bar than cross-repo echoes
            if not cross and score < max(threshold, 0.90):
                continue
            if prefer_cross and not cross:
                continue
            action = (
                SuggestedAction.DELETE_DUPLICATE
                if score >= 0.92
                else SuggestedAction.EXTRACT_SHARED
            )
            if action == SuggestedAction.EXTRACT_SHARED and min(_loc(a), _loc(b)) >= 15:
                # Winnowing is rename-resistant; cross-repo domain vocab drift is expected.
                if not cross and (
                    domain_token_divergence(a.source_snippet or "", b.source_snippet or "")
                    >= _DOMAIN_DIVERGENCE_SKIP
                ):
                    continue
            sev = Severity.HIGH if cross or score >= 0.88 else Severity.MEDIUM
            title = (
                f"Structural clone ({score:.0%}): "
                f"{name_a} ↔ {name_b}"
            )
            evidence = (
                f"Winnowing Jaccard={score:.3f} between "
                f"{a.repo}:{a.path}::{a.qualified_name} and "
                f"{b.repo}:{b.path}::{b.qualified_name} "
                f"(rename-resistant; typical AI reimplementation signal)."
            )
            # Rank: cross-repo first, then jaccard
            rank = score + (0.2 if cross else 0.0) + (0.05 if name_a != name_b else 0.0)
            f = try_make_finding(
                id=content_hash(f"fp:{a_id}:{b_id}:{score:.3f}"),
                goal=self.goal,
                sensor=self.id,
                severity=sev,
                title=title[:200],
                evidence=evidence,
                unit_ids=[a_id, b_id],
                action_hint=action,
                score=min(0.99, 0.55 + score * 0.45 + (0.08 if cross else 0.0)),
                metrics={
                    "jaccard": score,
                    "cross_repo": 1.0 if cross else 0.0,
                    "loc_a": float(_loc(a)),
                    "loc_b": float(_loc(b)),
                },
                metadata={"detector": "winnowing", "cross_repo": cross},
                known_ids=known,
            )
            if f:
                scored.append((rank, f))
        scored.sort(key=lambda t: -t[0])
        return [f for _, f in scored[:max_pairs]]


def fingerprint_clone() -> Sensor:
    return FingerprintCloneSensor()
