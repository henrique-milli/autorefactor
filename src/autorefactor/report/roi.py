"""ROI / debt / value-score helpers for report storytelling (estimates only)."""

from __future__ import annotations

from typing import Any

from autorefactor.changesets.models import Changeset

_DEDUP_ACTIONS = frozenset({"merge", "extract_shared", "delete_duplicate"})
_HIGH_CONFIDENCE = 0.85
TOKENS_PER_LOC = 4


def upside_kind(cs: Changeset) -> str:
    if cs.action in _DEDUP_ACTIONS:
        return "dedup"
    if cs.action == "ensure_tests":
        return "safety"
    if cs.action == "document":
        return "docs"
    return "structure"


def attempt_retries_avoided(cs: Changeset) -> float:
    """Coarse estimate of agent retry loops avoided by completing this task."""
    n_targets = max(1, len(cs.targets))
    conf = float(cs.confidence)
    if cs.action in _DEDUP_ACTIONS:
        # One shared fix instead of N fork patches
        return round(max(0.0, n_targets - 1) * (0.5 + 0.5 * conf), 2)
    if cs.action == "ensure_tests":
        return round(0.9 * conf, 2)
    if cs.action == "document":
        return round(0.35 * conf, 2)
    # split / structure — clearer locality, fewer thrash edits
    return round(0.45 * conf * min(2.0, 0.5 + 0.25 * n_targets), 2)


def per_task_agent_gains(
    changesets: list[Changeset],
    units_by_id: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Estimate agent economics framed as gains **per changeset/task**.

    Returns averages + ranked task rows. Token figures use ``TOKENS_PER_LOC``.
    """
    units_by_id = units_by_id or {}
    with_targets = [cs for cs in changesets if cs.targets]
    rows: list[dict[str, Any]] = []
    for cs in with_targets:
        rem = int(cs.metrics_delta.get("estimated_loc_removed") or 0)
        rev = int(cs.metrics_delta.get("loc_under_review") or 0)
        blast_chars = 0
        for t in cs.targets:
            u = units_by_id.get(t.unit_id or "")
            if u is not None:
                blast_chars += len(getattr(u, "source_snippet", None) or "")
        if blast_chars <= 0 and rev > 0:
            # Weaker fallback than real snippets — avoids vanity-split inflation
            blast_chars = rev * 20
        blast_tok = max(0, blast_chars // TOKENS_PER_LOC)
        dedup_tok = rem * TOKENS_PER_LOC
        retries = attempt_retries_avoided(cs)
        # Prefer removable LOC + retries; blast is secondary context load
        gain_tokens = int(2.0 * dedup_tok + 0.12 * blast_tok + 180 * retries)
        rows.append(
            {
                "id": cs.id,
                "title": cs.title,
                "action": cs.action,
                "confidence": cs.confidence,
                "targets": len(cs.targets),
                "estimated_loc_removed": rem,
                "loc_under_review": rev,
                "blast_tokens": blast_tok,
                "dedup_tokens_saved": dedup_tok,
                "retries_avoided": retries,
                "gain_tokens": gain_tokens,
                "upside_kind": upside_kind(cs),
            }
        )
    rows.sort(
        key=lambda r: (
            -int(r.get("gain_tokens") or 0),
            -float(r.get("retries_avoided") or 0),
            -float(r.get("confidence") or 0),
        )
    )
    n = len(rows) or 1
    avg = {
        "gain_tokens": round(sum(int(r["gain_tokens"]) for r in rows) / n) if rows else 0,
        "blast_tokens": round(sum(int(r["blast_tokens"]) for r in rows) / n) if rows else 0,
        "dedup_tokens_saved": round(sum(int(r["dedup_tokens_saved"]) for r in rows) / n) if rows else 0,
        "retries_avoided": round(sum(float(r["retries_avoided"]) for r in rows) / n, 2)
        if rows
        else 0.0,
        "tasks": len(rows),
    }
    return {
        "label": "estimate",
        "method": (
            "Per task: dedup_tokens=removable_LOC×4; blast_tokens=target snippet chars÷4; "
            "retries_avoided≈dedup (N−1)×conf, ensure_tests 0.9×conf, split 0.45×conf; "
            "gain_tokens=2×dedup + 0.12×blast + 180×retries"
        ),
        "averages": avg,
        "tasks": rows,
        "top_ids": [str(r["id"]) for r in rows[:8]],
    }


def plain_english_upside(cs: Changeset) -> str:
    rem = int(cs.metrics_delta.get("estimated_loc_removed") or 0)
    rev = int(cs.metrics_delta.get("loc_under_review") or 0)
    kind = upside_kind(cs)
    if kind == "dedup" and rem > 0:
        return f"Share/delete ~{rem} LOC of duplicated logic (less fork surface for agents)."
    if kind == "dedup":
        return "Consolidate near-duplicate behavior so agents edit one place."
    if kind == "safety":
        return "Add characterization coverage so later refactors land safer."
    if kind == "docs":
        return "Fill doc gaps so agents (and humans) infer intent without guessing."
    # structure / split
    return (
        f"Reshape ~{rev} LOC for clearer boundaries — structure / agent locality "
        "(not a LOC-delete win)."
    )


def value_score(
    cs: Changeset,
    *,
    max_rem: float,
    max_rev: float,
) -> float:
    """Narrative value: prefer confident dedup over vanity splits.

    0.45*conf + 0.35*norm(est_loc_removed) + 0.15*cross_repo + 0.05*norm(loc_under_review)
    −0.15 when action=split with 0 removable and no needs_coverage follow-up signal.
    """
    rem = float(int(cs.metrics_delta.get("estimated_loc_removed") or 0))
    rev = float(int(cs.metrics_delta.get("loc_under_review") or 0))
    norm_rem = (rem / max_rem) if max_rem > 0 else 0.0
    norm_rev = (rev / max_rev) if max_rev > 0 else 0.0
    cross = 1.0 if bool(cs.metrics_delta.get("cross_repo")) or len({t.repo for t in cs.targets if t.repo}) >= 2 else 0.0
    score = (
        0.45 * float(cs.confidence)
        + 0.35 * norm_rem
        + 0.15 * cross
        + 0.05 * norm_rev
    )
    needs_cov = bool(cs.metrics_delta.get("needs_coverage"))
    if cs.action == "split" and rem <= 0 and not needs_cov:
        score -= 0.15
    return max(0.0, min(1.0, score))


def attempt_savings_band(
    *,
    high_conf_dedup: int,
    dedup_cs: int,
    split_cs: int,
    test_cs: int,
    queue_n: int,
) -> dict:
    """Coarse estimate of agent-loop savings from working the queue."""
    if queue_n <= 0:
        band = "none"
    elif high_conf_dedup >= 2 or (dedup_cs >= 3 and high_conf_dedup >= 1):
        band = "high"
    elif dedup_cs >= 1 or test_cs >= 1:
        band = "medium"
    elif split_cs > 0 and dedup_cs == 0:
        band = "low"
    else:
        band = "low"
    return {
        "value": band,
        "label": "estimate",
        "method": (
            "high = ≥2 high-conf dedup (or 3+ dedup with ≥1 high-conf); "
            "medium = any dedup/ensure_tests; low = split-heavy or thin queue"
        ),
    }


def compute_debt_components(
    *,
    split_cs: int,
    clone_paths: int,
    test_cs: int,
    doc_cs: int,
    slop_score: float,
    queue_n: int,
    findings_structure: int,
    findings_clone: int,
    coverage_estimated: bool = False,
    test_inventory_ratio: float = 0.0,
) -> dict[str, float]:
    """0–100 sub-scores for technical debt (estimate)."""
    # Scale with presence; clamp
    complexity = min(100.0, 12.0 * split_cs + 3.0 * findings_structure)
    clone = min(100.0, 18.0 * clone_paths + 8.0 * findings_clone)
    if coverage_estimated:
        # No indexed Jacoco files — cap low using test-inventory heuristics only
        coverage = min(
            14.0,
            6.0 * test_cs + 8.0 * min(1.0, float(test_inventory_ratio or 0.0)),
        )
    else:
        coverage = min(100.0, 25.0 * test_cs) if test_cs else 0.0
    # Splits without ensure_tests → mild coverage-gap pressure
    if split_cs and test_cs == 0 and queue_n:
        coverage = max(coverage, 35.0)
    docs = min(100.0, 22.0 * doc_cs)
    slop = min(100.0, float(slop_score or 0.0))
    return {
        "complexity": round(complexity, 1),
        "clone": round(clone, 1),
        "coverage": round(coverage, 1),
        "docs": round(docs, 1),
        "slop": round(slop, 1),
    }


def composite_debt_score(components: dict[str, float]) -> float:
    """Weighted debt 0–100."""
    w = {
        "complexity": 0.25,
        "clone": 0.30,
        "coverage": 0.20,
        "docs": 0.10,
        "slop": 0.15,
    }
    total = sum(w[k] * float(components.get(k) or 0.0) for k in w)
    return round(min(100.0, total), 1)


def agent_friction_score(
    *,
    debt_components: dict[str, float],
    clone_paths: int,
    split_cs: int,
    test_cs: int,
) -> float:
    """Higher = harder for agents to change correctly (estimate)."""
    base = (
        0.35 * float(debt_components.get("clone") or 0.0)
        + 0.30 * float(debt_components.get("complexity") or 0.0)
        + 0.25 * float(debt_components.get("coverage") or 0.0)
        + 0.10 * float(debt_components.get("slop") or 0.0)
    )
    # Missing tests while splitting raises friction
    if split_cs and test_cs == 0:
        base = min(100.0, base + 10.0)
    if clone_paths >= 3:
        base = min(100.0, base + 5.0)
    return round(min(100.0, base), 1)


def enrich_changeset_rows(rows: list[dict], changesets: list[Changeset]) -> list[dict]:
    """Attach value_score, upside_kind, plain_english; return value-ranked copy of rows."""
    by_id = {cs.id: cs for cs in changesets}
    max_rem = max((int(r.get("estimated_loc_removed") or 0) for r in rows), default=0)
    max_rev = max((int(r.get("loc_under_review") or 0) for r in rows), default=0)
    out: list[dict] = []
    for row in rows:
        cs = by_id.get(str(row.get("id") or ""))
        if cs is None:
            # synthesize minimal fields
            kind = "structure"
            if row.get("action") in _DEDUP_ACTIONS:
                kind = "dedup"
            elif row.get("action") == "ensure_tests":
                kind = "safety"
            elif row.get("action") == "document":
                kind = "docs"
            enriched = dict(row)
            enriched["upside_kind"] = kind
            enriched["value_score"] = float(row.get("confidence") or 0)
            enriched["plain_english"] = row.get("upside") or ""
            out.append(enriched)
            continue
        vs = value_score(cs, max_rem=float(max_rem), max_rev=float(max_rev))
        enriched = dict(row)
        enriched["value_score"] = round(vs, 4)
        enriched["upside_kind"] = upside_kind(cs)
        enriched["plain_english"] = plain_english_upside(cs)
        enriched["upside"] = plain_english_upside(cs)
        out.append(enriched)
    out.sort(
        key=lambda r: (
            -float(r.get("value_score") or 0),
            -float(r.get("confidence") or 0),
            -int(r.get("estimated_loc_removed") or 0),
        )
    )
    return out
