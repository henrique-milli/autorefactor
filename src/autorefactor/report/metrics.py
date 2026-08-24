from __future__ import annotations

from collections import Counter
from pathlib import Path

from autorefactor.changesets.models import Changeset, ChangesetStatus
from autorefactor.domain import CodeUnit, ConfirmedFinding, CoverageSnapshot, Finding, SensorReport
from autorefactor.testing.inventory import inventory_test_files, looks_like_test_path
from autorefactor.report.roi import (
    TOKENS_PER_LOC,
    agent_friction_score,
    attempt_savings_band,
    composite_debt_score,
    compute_debt_components,
    enrich_changeset_rows,
    per_task_agent_gains,
    plain_english_upside,
)

_DEDUP_ACTIONS = frozenset({"merge", "extract_shared", "delete_duplicate"})
_HIGH_CONFIDENCE = 0.85


def _changeset_repos(cs: Changeset) -> set[str]:
    return {t.repo for t in cs.targets if t.repo}


def estimate_metrics(
    *,
    units: list[CodeUnit],
    findings: list[Finding] | None = None,
    confirmed: list[ConfirmedFinding] | None = None,
    changesets: list[Changeset],
    flags: list[Finding] | None = None,
    findings_confirmed: list[ConfirmedFinding] | None = None,
    auto_rejected_count: int = 0,
    auto_rejected_flag_count: int = 0,
    quality_warnings: list[str] | None = None,
    raw_finding_count: int | None = None,
    raw_flag_count: int | None = None,
    coverage: CoverageSnapshot | None = None,
    workspace: Path | None = None,
    repos: list[Path] | None = None,
    pipeline_stats: dict | None = None,
) -> dict:
    findings = findings if findings is not None else (flags or [])
    confirmed = confirmed if confirmed is not None else (findings_confirmed or [])
    auto_rejected = auto_rejected_count or auto_rejected_flag_count
    raw_count = raw_finding_count if raw_finding_count is not None else raw_flag_count

    files = {(u.repo, u.path) for u in units}
    repos_scanned = sorted({u.repo for u in units if u.repo})
    loc = 0
    for unit in units:
        if unit.kind.value == "file":
            loc += max(1, unit.span.end_line or unit.source_snippet.count("\n") + 1)
    if loc == 0:
        loc = sum(
            max(1, u.source_snippet.count("\n") + 1)
            for u in units
            if u.kind.value == "function"
        )

    valid = [f for f in confirmed if f.valid]
    trashed = [
        f
        for f in confirmed
        if (f.disposition or ("trash" if not f.valid else None)) == "trash"
        or (not f.valid and f.disposition != "review")
    ]
    dropped_plan = [
        f
        for f in confirmed
        if not f.valid and "Dropped during plan" in (f.reasoning or "")
    ]
    with_targets = [cs for cs in changesets if cs.targets]
    est_loc = sum(int(cs.metrics_delta.get("estimated_loc_removed") or 0) for cs in with_targets)
    loc_under_review = sum(int(cs.metrics_delta.get("loc_under_review") or 0) for cs in with_targets)
    baseline_loc = max(1, loc)
    loc_pct = 100.0 * est_loc / baseline_loc
    review_pct = 100.0 * loc_under_review / baseline_loc
    est_tokens = est_loc * TOKENS_PER_LOC
    baseline_tokens = max(1, sum(len(u.source_snippet) for u in units) // TOKENS_PER_LOC)
    token_pct = 100.0 * est_tokens / baseline_tokens

    # Blast-radius tokens from changeset target snippets
    units_by_id = {u.id: u for u in units}
    blast_chars = 0
    for cs in with_targets:
        for t in cs.targets:
            u = units_by_id.get(t.unit_id or "")
            if u:
                blast_chars += len(u.source_snippet or "")
    context_tokens_blast = max(0, blast_chars // TOKENS_PER_LOC)
    context_tokens_dedup = est_loc * TOKENS_PER_LOC

    dup_paths = {
        t.path
        for cs in with_targets
        if cs.action in _DEDUP_ACTIONS
        for t in cs.targets
    }
    blast_paths = {t.path for cs in with_targets for t in cs.targets}

    by_goal = Counter(f.goal.value for f in findings)
    cs_by_goal = Counter(str(cs.metrics_delta.get("goal") or "unknown") for cs in with_targets)
    by_action = Counter(cs.action for cs in with_targets)

    high_conf = sum(1 for cs in with_targets if cs.confidence >= _HIGH_CONFIDENCE)
    cross_repo = sum(1 for cs in with_targets if len(_changeset_repos(cs)) >= 2)
    dedup_cs = sum(1 for cs in with_targets if cs.action in _DEDUP_ACTIONS)
    split_cs = sum(1 for cs in with_targets if cs.action == "split")
    test_cs = sum(1 for cs in with_targets if cs.action == "ensure_tests")
    planner_coverage = 100.0 * len(valid) / max(1, len(findings)) if findings else 100.0
    # "Dropped during plan" is selection gap, not sensor false-positive
    evidence_rejected = max(0, len(confirmed) - len(valid) - len(dropped_plan))

    slop_findings = [f for f in findings if f.goal.value == "slop" or f.sensor == "slop"]
    fp_findings = [
        f
        for f in findings
        if f.goal.value == "structural_clone" or f.sensor == "sim.fingerprint"
    ]
    slop_score = 0.0
    for f in slop_findings:
        slop_score = max(slop_score, float((f.metrics or {}).get("slop_score") or 0.0))
    if slop_score <= 0 and fp_findings:
        # Derive a light score from structural clone density when slop sensor quiet
        slop_score = min(100.0, 15.0 + 4.0 * len(fp_findings))

    cross_cs = [cs for cs in with_targets if len(_changeset_repos(cs)) >= 2]
    cross_loc_review = sum(int(cs.metrics_delta.get("loc_under_review") or 0) for cs in cross_cs)
    cross_loc_removed = sum(int(cs.metrics_delta.get("estimated_loc_removed") or 0) for cs in cross_cs)
    high_conf_cross = sum(1 for cs in cross_cs if cs.confidence >= _HIGH_CONFIDENCE)
    dedup_loc = sum(
        int(cs.metrics_delta.get("estimated_loc_removed") or 0)
        for cs in with_targets
        if cs.action in _DEDUP_ACTIONS
    )
    high_conf_dedup = sum(
        1 for cs in with_targets if cs.action in _DEDUP_ACTIONS and cs.confidence >= _HIGH_CONFIDENCE
    )

    upside_bullets = _build_upside_bullets(
        queue_n=len(with_targets),
        high_conf=high_conf,
        est_loc=est_loc,
        loc_pct=loc_pct,
        loc_under_review=loc_under_review,
        review_pct=review_pct,
        baseline_loc=baseline_loc,
        cross_repo=cross_repo,
        cross_loc_review=cross_loc_review,
        cross_loc_removed=cross_loc_removed,
        high_conf_cross=high_conf_cross,
        dedup_cs=dedup_cs,
        dedup_loc=dedup_loc,
        high_conf_dedup=high_conf_dedup,
        split_cs=split_cs,
        test_cs=test_cs,
        slop_score=slop_score,
        clone_paths=len(dup_paths),
        blast_files=len(blast_paths),
        repos_n=len(repos_scanned),
        token_pct=token_pct,
    )
    pitch = _build_upside_pitch(
        queue_n=len(with_targets),
        est_loc=est_loc,
        loc_under_review=loc_under_review,
        cross_repo=cross_repo,
        high_conf=high_conf,
        slop_score=slop_score,
        repos_n=len(repos_scanned),
    )

    headline_parts = [
        f"**{len(with_targets)}** agent-ready changesets",
        f"**{loc_under_review:,}** LOC under review",
    ]
    if est_loc > 0:
        headline_parts.append(f"**~{est_loc:,}** LOC dedup upside")
    if findings:
        headline_parts.append(f"**{planner_coverage:.0f}%** planner coverage")
    if slop_score >= 20:
        headline_parts.append(f"**slop {slop_score:.0f}/100**")
    if cross_repo:
        headline_parts.append(f"**{cross_repo}** cross-repo opportunities")
    elif dedup_cs:
        headline_parts.append(f"**{dedup_cs}** dedup / share actions")
    headline = " · ".join(headline_parts)

    coverage_estimated = bool(
        coverage and (coverage.source or "").lower() == "estimated" and not coverage.files
    )
    production_units = sum(
        1
        for u in units
        if u.kind.value in {"function", "type"}
        and not looks_like_test_path(u.path)
        and "src/test/" not in u.path.replace("\\", "/").lower()
    )
    test_file_count = 0
    if repos:
        test_file_count = len(inventory_test_files(repos))
    elif workspace and workspace.is_dir():
        child_repos = [
            p
            for p in sorted(workspace.iterdir())
            if p.is_dir()
            and not p.name.startswith(".")
            and any((p / m).exists() for m in ("pom.xml", "pyproject.toml", "go.mod", "package.json"))
        ]
        if child_repos:
            test_file_count = len(inventory_test_files(child_repos))
    test_inventory_ratio = test_file_count / max(1, production_units)

    warnings = list(quality_warnings or [])
    if coverage_estimated:
        indexed = len(coverage.files) if coverage else 0
        warnings.append(
            f"Coverage estimated only ({indexed} Jacoco files indexed)—"
            "run mvn test jacoco:report before trusting ensure_tests prep"
        )
        gen_note = (coverage.summary or "") if coverage else ""
        if "Coverage generate:" in gen_note:
            warnings.append(gen_note.split("Coverage generate:", 1)[-1].strip())
    if not with_targets:
        warnings.append("Queue empty after planning — no structurally valid decisions.")
    elif (
        est_loc < 20
        and loc_under_review < 200
        and not any(cs.action == "ensure_tests" for cs in with_targets)
    ):
        warnings.append(f"Total estimated LOC upside is only {est_loc}.")
    if dropped_plan:
        warnings.append(
            f"{len(dropped_plan)} findings were not selected into any plan decision "
            "(oversized conflict component or incomplete LLM coverage)."
        )

    per_cs = [
        {
            "id": cs.id,
            "title": cs.title,
            "action": cs.action,
            "goal": cs.metrics_delta.get("goal"),
            "estimated_loc_removed": int(cs.metrics_delta.get("estimated_loc_removed") or 0),
            "loc_under_review": int(cs.metrics_delta.get("loc_under_review") or 0),
            "confidence": cs.confidence,
            "targets": len(cs.targets),
            "repos": sorted(_changeset_repos(cs)),
            "cross_repo": len(_changeset_repos(cs)) >= 2,
            "upside": plain_english_upside(cs),
            "needs_coverage": bool(cs.metrics_delta.get("needs_coverage")),
        }
        for cs in with_targets
    ]
    per_cs = enrich_changeset_rows(per_cs, with_targets)
    value_ranked_ids = [str(r["id"]) for r in per_cs]

    doc_cs = sum(1 for cs in with_targets if cs.action == "document")
    findings_structure = by_goal.get("structure", 0) + by_goal.get("static", 0)
    findings_clone = (
        by_goal.get("method_similarity", 0)
        + by_goal.get("class_similarity", 0)
        + by_goal.get("structural_clone", 0)
        + by_goal.get("slop", 0)
    )
    debt_components = compute_debt_components(
        split_cs=split_cs,
        clone_paths=len(dup_paths),
        test_cs=test_cs,
        doc_cs=doc_cs,
        slop_score=slop_score,
        queue_n=len(with_targets),
        findings_structure=findings_structure,
        findings_clone=findings_clone,
        coverage_estimated=coverage_estimated,
        test_inventory_ratio=test_inventory_ratio,
    )
    debt_score = composite_debt_score(debt_components)
    friction = agent_friction_score(
        debt_components=debt_components,
        clone_paths=len(dup_paths),
        split_cs=split_cs,
        test_cs=test_cs,
    )
    attempts = attempt_savings_band(
        high_conf_dedup=high_conf_dedup,
        dedup_cs=dedup_cs,
        split_cs=split_cs,
        test_cs=test_cs,
        queue_n=len(with_targets),
    )
    per_task = per_task_agent_gains(with_targets, units_by_id)

    roi = {
        "debt_score": {
            "value": debt_score,
            "label": "estimate",
            "method": (
                "0.25*complexity + 0.30*clone + 0.20*coverage + 0.10*docs + 0.15*slop "
                "(each component 0–100 from queue/finding pressure)"
            ),
            "components": debt_components,
        },
        "agent_friction": {
            "value": friction,
            "label": "estimate",
            "method": (
                "0.35*clone + 0.30*complexity + 0.25*coverage + 0.10*slop; "
                "+10 if splits without ensure_tests; +5 if ≥3 clone paths"
            ),
        },
        "context_tokens_blast_radius": {
            "value": context_tokens_blast,
            "label": "estimate",
            "method": "sum(len(target.source_snippet))/4 over queued targets",
        },
        "context_tokens_dedup_saved": {
            "value": context_tokens_dedup,
            "label": "estimate",
            "method": "estimated_loc_removed * 4 tokens/LOC",
        },
        "attempt_savings_band": attempts,
        "per_task_gains": per_task,
        "value_ranked_changeset_ids": value_ranked_ids,
    }

    return {
        "exact": {
            "unit_count": len(units),
            "file_count": len(files),
            "loc": loc,
            "repos_scanned": repos_scanned,
            "flag_count": len(findings),
            "finding_count": len(findings),
            "raw_flag_count": raw_count if raw_count is not None else len(findings),
            "auto_rejected_flag_count": auto_rejected,
            "confirmed_count": len(valid),
            "planner_trash_count": len(trashed),
            "confirmation_rate_percent": round(planner_coverage, 1),
            "planner_coverage_percent": round(planner_coverage, 1),
            "planner_dropped_count": len(dropped_plan),
            "evidence_rejected_count": evidence_rejected,
            "changeset_count": len(changesets),
            "changesets_with_targets": len(with_targets),
            "high_confidence_changeset_count": high_conf,
            "cross_repo_changeset_count": cross_repo,
            "dedup_changeset_count": dedup_cs,
            "split_changeset_count": split_cs,
            "ensure_tests_changeset_count": test_cs,
            "similarity_flag_count": by_goal.get("method_similarity", 0)
            + by_goal.get("class_similarity", 0)
            + by_goal.get("structural_clone", 0),
            "structural_clone_count": by_goal.get("structural_clone", 0),
            "slop_finding_count": by_goal.get("slop", 0),
            "slop_score": round(slop_score, 1),
            "oss_flag_count": by_goal.get("structure", 0) + by_goal.get("static", 0),
            "host_skipped": sum(1 for cs in changesets if cs.status == ChangesetStatus.SKIPPED),
            "host_done": sum(1 for cs in changesets if cs.status == ChangesetStatus.DONE),
            "flags_by_goal": dict(by_goal),
            "changesets_by_goal": dict(cs_by_goal),
            "changesets_by_action": dict(by_action),
        },
        "impact": {
            "headline": headline,
            "pitch": pitch,
            "upside_bullets": upside_bullets,
            "agent_ready_changesets": len(with_targets),
            "high_confidence_changesets": high_conf,
            "confirmation_rate_percent": round(planner_coverage, 1),
            "planner_coverage_percent": round(planner_coverage, 1),
            "planner_dropped_count": len(dropped_plan),
            "loc_under_review": loc_under_review,
            "loc_under_review_percent_of_baseline": round(review_pct, 1),
            "estimated_loc_removed": est_loc,
            "estimated_loc_removed_percent_of_baseline": round(loc_pct, 2),
            "files_in_blast_radius": len(blast_paths),
            "repos_in_scope": len(repos_scanned),
            "cross_repo_opportunities": cross_repo,
            "cross_repo_loc_under_review": cross_loc_review,
            "cross_repo_estimated_loc_removed": cross_loc_removed,
            "high_confidence_cross_repo": high_conf_cross,
            "dedup_share_delete_track": dedup_cs,
            "dedup_estimated_loc_removed": dedup_loc,
            "high_confidence_dedup": high_conf_dedup,
            "complexity_split_track": split_cs,
            "coverage_test_gap_track": test_cs,
            "clone_paths_at_risk": len(dup_paths),
            "slop_score": round(slop_score, 1),
            "structural_clone_findings": len(fp_findings),
        },
        "estimates": {
            "expected_loc_reduction": {
                "value": est_loc,
                "percent_of_baseline": round(loc_pct, 2),
                "label": "estimate",
                "method": "sum of per-changeset removable LOC (dedup track)",
            },
            "loc_under_review": {
                "value": loc_under_review,
                "percent_of_baseline": round(review_pct, 1),
                "label": "exact_from_targets",
                "method": "sum of loc_under_review on queued changesets",
            },
            "expected_token_reduction_percent": {
                "value": round(token_pct, 2),
                "label": "estimate",
                "method": "removable_loc * 4 / baseline snippet tokens",
            },
            "upside_by_goal": {
                goal: sum(
                    int(cs.metrics_delta.get("estimated_loc_removed") or 0)
                    for cs in with_targets
                    if str(cs.metrics_delta.get("goal") or "unknown") == goal
                )
                for goal in cs_by_goal
            },
            "upside_by_action": {
                action: {
                    "changesets": count,
                    "estimated_loc_removed": sum(
                        int(cs.metrics_delta.get("estimated_loc_removed") or 0)
                        for cs in with_targets
                        if cs.action == action
                    ),
                    "loc_under_review": sum(
                        int(cs.metrics_delta.get("loc_under_review") or 0)
                        for cs in with_targets
                        if cs.action == action
                    ),
                }
                for action, count in by_action.items()
            },
        },
        "scores": {
            "tech_debt": {
                "value": debt_score,
                "label": "estimate",
                "method": "composite debt_score (see roi.debt_score)",
            },
            "risk_surface_duplicate_paths": {
                "value": len(dup_paths),
                "label": "count",
                "method": "distinct paths targeted by merge/extract/delete",
            },
        },
        "roi": roi,
        "upside_by_changeset": per_cs,
        "quality_warnings": warnings,
        "pipeline": pipeline_stats or {},
    }


def _build_upside_pitch(
    *,
    queue_n: int,
    est_loc: int,
    loc_under_review: int,
    cross_repo: int,
    high_conf: int,
    slop_score: float,
    repos_n: int,
) -> str:
    if queue_n <= 0:
        return "No agent-ready refactor upsides in the queue yet."
    bits: list[str] = []
    if est_loc > 0:
        bits.append(f"delete or share roughly **{est_loc:,} LOC** of duplicated logic")
    if cross_repo:
        bits.append(f"consolidate **{cross_repo}** cross-repo echoes")
    if loc_under_review > 0:
        bits.append(f"put **{loc_under_review:,} LOC** of debt under a ranked agent queue")
    if high_conf:
        bits.append(f"**{high_conf}** high-confidence tasks ready first")
    if slop_score >= 20:
        bits.append(f"cut AI-slop pressure (score **{slop_score:.0f}/100**)")
    if not bits:
        bits.append(f"execute **{queue_n}** ranked changesets across **{repos_n}** repo(s)")
    return "Executing this queue would " + "; ".join(bits) + "."


def _build_upside_bullets(
    *,
    queue_n: int,
    high_conf: int,
    est_loc: int,
    loc_pct: float,
    loc_under_review: int,
    review_pct: float,
    baseline_loc: int,
    cross_repo: int,
    cross_loc_review: int,
    cross_loc_removed: int,
    high_conf_cross: int,
    dedup_cs: int,
    dedup_loc: int,
    high_conf_dedup: int,
    split_cs: int,
    test_cs: int,
    slop_score: float,
    clone_paths: int,
    blast_files: int,
    repos_n: int,
    token_pct: float,
) -> list[str]:
    if queue_n <= 0:
        return ["Queue empty — no refactor upside to claim yet."]
    bullets: list[str] = []
    if est_loc > 0:
        bullets.append(
            f"**Direct code reduction:** ~**{est_loc:,} LOC** "
            f"({loc_pct:.1f}% of ~{baseline_loc:,} LOC) removable via merge / extract / delete "
            f"({high_conf_dedup} of {dedup_cs} dedup tasks are high-confidence)."
        )
    if cross_repo:
        bullets.append(
            f"**Cross-repo leverage:** **{cross_repo}** opportunities spanning shared logic "
            f"(~{cross_loc_removed:,} LOC dedup upside; ~{cross_loc_review:,} LOC in those edits; "
            f"{high_conf_cross} high-confidence). One shared fix beats N copy-paste drifts."
        )
    if loc_under_review > 0:
        bullets.append(
            f"**Debt in scope:** **{loc_under_review:,} LOC** "
            f"({review_pct:.0f}% of baseline) across **{blast_files}** files / **{repos_n}** repo(s) "
            f"is already packaged into **{queue_n}** agent-ready changesets "
            f"({high_conf} ≥ {_HIGH_CONFIDENCE:.0%} confidence)."
        )
    if clone_paths:
        bullets.append(
            f"**Clone surface:** **{clone_paths}** paths sit on the dedup track — "
            "fewer places for bugs and AI reimplementation to fork."
        )
    if slop_score >= 20:
        bullets.append(
            f"**AI-slop pressure:** workspace slop score **{slop_score:.0f}/100**. "
            "Working the queue replaces “write another copy” with extract/delete/share."
        )
    if split_cs:
        bullets.append(
            f"**Complexity relief:** **{split_cs}** split tasks shrink god-modules / long units "
            "so future agent edits stay local."
        )
    if test_cs:
        bullets.append(
            f"**Safer landing:** **{test_cs}** ensure_tests items close coverage gaps "
            "before or beside structural edits."
        )
    if token_pct > 0 and est_loc > 0:
        bullets.append(
            f"**Context savings (estimate):** ~**{token_pct:.1f}%** fewer tokens in duplicated "
            "snippets after the conservative dedup track lands."
        )
    if not bullets:
        bullets.append(
            f"**{queue_n}** ranked changesets are ready for the host agent "
            f"({high_conf} high-confidence)."
        )
    return bullets


def warnings_from_analyzer_notes(notes: list[SensorReport]) -> list[str]:
    out: list[str] = []
    for note in notes:
        label = note.sensor or note.name
        if note.status == "skipped":
            out.append(f"Sensor '{label}' skipped: {note.detail or 'unavailable'}")
        elif note.status == "error":
            out.append(f"Sensor '{label}' error: {note.detail}")
    return out


def write_metrics_and_report(
    report_dir: Path,
    *,
    metrics: dict,
    changesets: list[Changeset],
    visual_paths: list[str],
) -> None:
    report_dir.mkdir(parents=True, exist_ok=True)
    import json

    (report_dir / "metrics.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    exact = metrics["exact"]
    impact = metrics.get("impact") or {}
    roi = metrics.get("roi") or {}
    est = metrics["estimates"]["expected_loc_reduction"]
    review = metrics["estimates"].get("loc_under_review") or {
        "value": impact.get("loc_under_review", 0),
        "percent_of_baseline": impact.get("loc_under_review_percent_of_baseline", 0),
    }

    debt_block = roi.get("debt_score") or {}
    debt_score = float(debt_block.get("value") or metrics.get("scores", {}).get("tech_debt", {}).get("value") or 0)
    components = debt_block.get("components") or {}
    friction = float((roi.get("agent_friction") or {}).get("value") or 0)
    blast_tok = int((roi.get("context_tokens_blast_radius") or {}).get("value") or 0)
    dedup_tok = int((roi.get("context_tokens_dedup_saved") or {}).get("value") or 0)
    attempts = roi.get("attempt_savings_band") or {}
    attempt_band = str(attempts.get("value") or "none")
    per_task = roi.get("per_task_gains") or {}
    per_avg = per_task.get("averages") or {}
    per_rows = list(per_task.get("tasks") or [])[:5]

    rows = list(metrics.get("upside_by_changeset") or [])
    # Prefer value ranking already applied by enrich_changeset_rows
    value_ids = list(roi.get("value_ranked_changeset_ids") or [])
    if value_ids:
        by_id = {str(r.get("id")): r for r in rows}
        ordered = [by_id[i] for i in value_ids if i in by_id]
        for r in rows:
            if str(r.get("id")) not in value_ids:
                ordered.append(r)
        rows = ordered

    est_loc = int(impact.get("estimated_loc_removed", est.get("value", 0)) or 0)
    baseline_loc = max(1, int(exact.get("loc") or 1))
    loc_tiny = est_loc < 0.01 * baseline_loc

    top3 = rows[:3]
    lines: list[str] = [
        "# autorefactor analysis report",
        "",
        "## Executive brief",
        "",
    ]
    if loc_tiny:
        lines.append(
            f"- **Technical debt (estimate):** **{debt_score:.0f}/100** · "
            f"**Agent friction (estimate):** **{friction:.0f}/100**"
        )
        lines.append(
            f"- LOC delete upside is tiny (~**{est_loc}** / ~{baseline_loc:,} baseline) — "
            "lead with debt/friction and structure, not deletion."
        )
    else:
        lines.append(
            f"- **Technical debt (estimate):** **{debt_score:.0f}/100** · "
            f"**Agent friction (estimate):** **{friction:.0f}/100** · "
            f"~**{est_loc:,}** LOC dedup upside (estimate)"
        )
    if top3:
        lines.append("- **Top upsides:**")
        for row in top3:
            pe = row.get("plain_english") or row.get("upside") or "maintainability"
            lines.append(
                f"  - {pe} (`{row.get('action')}`, value {float(row.get('value_score') or 0):.2f})"
            )
    else:
        lines.append("- Queue empty — no ranked upsides yet.")
    lines.append(
        "- Soft metrics below are **estimates**; removable LOC is only the dedup track."
    )
    lines.append("")

    # Visuals early (after exec brief) with captions
    caption_by_name = {
        "embeddings_tsne.png": (
            "2D projection of unit embeddings; stars = queue targets; lines = clone-pair "
            "edges. Interactive 3D deferred to report UI."
        ),
        "debt_components.png": "Debt sub-scores (estimate): complexity, clone, coverage, docs, slop.",
        "upside_by_changeset.png": "Estimated value (not just confidence) — bars sorted by value_score.",
        "changesets_by_action.png": "Queue composition by action (secondary).",
        "changesets_by_goal.png": "Queue composition by analysis goal (secondary).",
    }
    preferred_visual_order = [
        "embeddings_tsne.png",
        "debt_components.png",
        "upside_by_changeset.png",
        "changesets_by_action.png",
        "changesets_by_goal.png",
    ]
    visual_names = [Path(p).name for p in visual_paths]
    ordered_visuals: list[str] = []
    for name in preferred_visual_order:
        if name in visual_names:
            ordered_visuals.append(name)
    for name in visual_names:
        if name not in ordered_visuals:
            ordered_visuals.append(name)

    lines.extend(["## Visuals", ""])
    if ordered_visuals:
        for name in ordered_visuals:
            lines.append(f"### {name}")
            lines.append("")
            cap = caption_by_name.get(name)
            if cap:
                lines.append(f"*{cap}*")
                lines.append("")
            lines.append(f"![{name}](visuals/{name})")
            lines.append("")
    else:
        lines.append("- (none)")
        lines.append("")

    lines.extend(
        [
            "## Agent economics",
            "",
            "Framed as **estimated gains per task** (then portfolio totals).",
            "",
            f"- **Avg gain / task (estimate):** ~**{int(per_avg.get('gain_tokens') or 0):,}** tok-eq · "
            f"~**{int(per_avg.get('dedup_tokens_saved') or 0):,}** dedup tok · "
            f"~**{float(per_avg.get('retries_avoided') or 0):.1f}** retries avoided · "
            f"**{int(per_avg.get('tasks') or 0)}** tasks",
            f"- **Portfolio blast radius (estimate):** ~**{blast_tok:,}** tokens across queued targets "
            f"(snippet chars ÷ {TOKENS_PER_LOC}).",
            f"- **Portfolio dedup token save (estimate):** ~**{dedup_tok:,}** tokens "
            f"(removable LOC × {TOKENS_PER_LOC}).",
            f"- **Attempt-savings band (estimate):** **{attempt_band}** — "
            f"{attempts.get('method') or 'coarse band from dedup vs split-heavy queues'}.",
            "",
        ]
    )
    if per_rows:
        lines.append("| Task | Gain (tok-eq) | Dedup tok | Retries | Action |")
        lines.append("|------|--------------:|----------:|--------:|--------|")
        for row in per_rows:
            title = str(row.get("title") or row.get("id") or "")[:48]
            lines.append(
                f"| {title} | **{int(row.get('gain_tokens') or 0):,}** | "
                f"{int(row.get('dedup_tokens_saved') or 0):,} | "
                f"{float(row.get('retries_avoided') or 0):.1f} | "
                f"`{row.get('action')}` |"
            )
        lines.append("")
    lines.extend(
        [
            "- Cleaner boundaries and fewer clones cut agent retries: less contradictory context, "
            "smaller blast radius, and one shared fix instead of N forks.",
            "",
            "## Technical debt snapshot",
            "",
            f"**Debt score (estimate):** **{debt_score:.0f}/100** "
            f"({debt_block.get('method') or 'weighted components'})",
            "",
            "| Component | Score |",
            "|-----------|------:|",
            f"| Complexity / split pressure | **{float(components.get('complexity') or 0):.0f}** |",
            f"| Clone surface | **{float(components.get('clone') or 0):.0f}** |",
            f"| Coverage gaps | **{float(components.get('coverage') or 0):.0f}** |",
            f"| Doc gaps | **{float(components.get('docs') or 0):.0f}** |",
            f"| Slop | **{float(components.get('slop') or 0):.0f}** |",
            "",
            f"**Agent friction (estimate):** **{friction:.0f}/100**",
            "",
        ]
    )
    coverage_debt = float(components.get("coverage") or 0)
    test_cs = int(exact.get("ensure_tests_changeset_count") or 0)
    if coverage_debt >= 30 or test_cs > 0:
        lines.extend(
            [
                "## Prep queue",
                "",
            ]
        )
        if coverage_debt >= 30:
            lines.append(
                f"- **Coverage-first ordering** is armed: elevated coverage debt "
                f"(**{coverage_debt:.0f}/100**) — walk `ensure_tests` prep before "
                "structural splits and document noise."
            )
        elif test_cs > 0:
            lines.append(
                "- **Coverage-first ordering:** `ensure_tests` items rank ahead of "
                "structural work and low-confidence document prep."
            )
        lines.append("")
    lines.extend(
        [
            "## Scoreboard",
            "",
            "| Metric | Value |",
            "|--------|------:|",
            f"| Ranked agent tasks | **{impact.get('agent_ready_changesets', exact.get('changesets_with_targets', 0))}** |",
            f"| High-confidence (≥{_HIGH_CONFIDENCE}) | **{impact.get('high_confidence_changesets', 0)}** |",
            f"| Est. LOC remove/share (estimate) | **{est_loc:,}** "
            f"({impact.get('estimated_loc_removed_percent_of_baseline', est.get('percent_of_baseline', 0))}% of ~{exact['loc']:,}) |",
            f"| LOC under review | **{int(review.get('value') or 0):,}** "
            f"({review.get('percent_of_baseline', 0)}% of baseline) |",
            f"| Cross-repo opportunities | **{impact.get('cross_repo_opportunities', 0)}** |",
            f"| Dedup / share / delete | **{impact.get('dedup_share_delete_track', 0)}** |",
            f"| Complexity splits | **{impact.get('complexity_split_track', 0)}** |",
            f"| Coverage / test-gap | **{impact.get('coverage_test_gap_track', 0)}** |",
            "",
            "## Top opportunities (by value score)",
            "",
        ]
    )
    top = rows[:12]
    if top:
        for i, row in enumerate(top, 1):
            pe = row.get("plain_english") or row.get("upside") or "maintainability"
            kind = row.get("upside_kind") or "structure"
            vs = float(row.get("value_score") or 0)
            cross = " · cross-repo" if row.get("cross_repo") else ""
            lines.append(
                f"{i}. {pe} — `{row.get('action')}` / `{kind}` "
                f"(conf {float(row.get('confidence') or 0):.2f}, value {vs:.2f}{cross})"
            )
    else:
        lines.append("- *(empty queue)*")

    lines.extend(
        [
            "",
            "## Full queue",
            "",
            "| ID | Action | Kind | Value | Conf | LOC review | Est. remove | Title |",
            "|----|--------|------|------:|-----:|-----------:|------------:|-------|",
        ]
    )
    for row in rows:
        lines.append(
            f"| `{row.get('id')}` | {row.get('action')} | {row.get('upside_kind') or '—'} | "
            f"{float(row.get('value_score') or 0):.2f} | {float(row.get('confidence') or 0):.2f} | "
            f"**{row.get('loc_under_review', 0)}** | {row.get('estimated_loc_removed', 0)} | "
            f"{(row.get('title') or '')[:48]} |"
        )
    if not rows:
        lines.append("| — | — | — | — | — | **0** | 0 | *(empty queue)* |")

    lines.extend(
        [
            "",
            "> Detail dumps live in `metrics.json` (`roi`, `upside_by_changeset`). "
            "Splits with 0 removable LOC are **structure / agent locality**, not delete wins.",
            "",
            "## Exact baseline",
            "",
            f"- Units: **{exact['unit_count']:,}** across **{exact.get('file_count', 0)}** files",
            f"- Baseline LOC: **~{exact['loc']:,}**",
        ]
    )
    repos = exact.get("repos_scanned") or []
    if repos:
        lines.append(f"- Repos: {', '.join(f'`{r}`' for r in repos)}")
    lines.extend(
        [
            f"- Findings: **{exact.get('finding_count', exact['flag_count'])}** "
            f"(raw {exact.get('raw_flag_count', exact['flag_count'])}, "
            f"auto-rejected {exact.get('auto_rejected_flag_count', 0)}; "
            f"confirmed **{exact['confirmed_count']}**)",
            f"- Planner-dropped: **{impact.get('planner_dropped_count', exact.get('planner_dropped_count', 0))}**",
            "",
            "## Quality warnings",
            "",
        ]
    )
    for w in metrics.get("quality_warnings") or []:
        lines.append(f"- {w}")
    if not metrics.get("quality_warnings"):
        lines.append("- No quality warnings.")
    lines.append("")

    (report_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")
    (report_dir / "report.json").write_text(
        json.dumps(
            {
                "metrics": metrics,
                "changeset_ids": [c.id for c in changesets],
                "visuals": [Path(p).name for p in visual_paths],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
