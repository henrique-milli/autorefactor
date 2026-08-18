from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn

from autorefactor.agenda import (
    build_planning_brief,
    build_unit_agenda,
    conflict_components,
    split_oversized_components,
)
from autorefactor.changesets.models import Changeset, ChangesetTarget
from autorefactor.domain import (
    AnalysisGoal,
    CodeUnit,
    ConfirmedFinding,
    Finding,
    PlannerDecision,
    PlanningBrief,
    SuggestedAction,
    UnitAgenda,
    UnitKind,
    content_hash,
)
from autorefactor.providers.base import LLMProvider
from autorefactor.sensors.validate import decision_structural_reason

logger = logging.getLogger(__name__)

_ARCHITECTURAL_WIRING_KINDS = frozenset({"orphan_kafka", "test_only_handler"})
_WIRING_CONFIDENCE_CAP = 0.84
_WIRING_DECISION_NOTE = (
    "Architectural Kafka wiring — choose wire_kafka_consumer (add @KafkaListener) "
    "or remove_dead_wiring (delete orphan handler and PSN config). "
    "Document-only prep cannot resolve this fork."
)


def _is_architectural_wiring_finding(finding: Finding) -> bool:
    meta = finding.metadata if isinstance(finding.metadata, dict) else {}
    return str(meta.get("kind") or "") in _ARCHITECTURAL_WIRING_KINDS


def _wiring_kind_from_findings(
    finding_ids: list[str],
    findings_by_id: dict[str, Finding],
) -> str | None:
    for fid in finding_ids:
        finding = findings_by_id.get(fid)
        if finding and _is_architectural_wiring_finding(finding):
            return str(finding.metadata.get("kind"))
    return None


def _apply_architectural_wiring_rules(
    decision: PlannerDecision,
    findings_by_id: dict[str, Finding],
) -> PlannerDecision:
    """Orphan Kafka / test-only handlers: integration decision brief, not document prep."""
    kind = _wiring_kind_from_findings(decision.finding_ids, findings_by_id)
    if not kind:
        return decision
    rationale = (decision.rationale or "").strip()
    if _WIRING_DECISION_NOTE not in rationale:
        rationale = f"{rationale}\n\n{_WIRING_DECISION_NOTE}".strip() if rationale else _WIRING_DECISION_NOTE
    return decision.model_copy(
        update={
            "action": SuggestedAction.ENSURE_TESTS,
            "confidence": min(decision.confidence, _WIRING_CONFIDENCE_CAP),
            "rationale": rationale,
        }
    )


def _decision_to_action(raw: str) -> SuggestedAction:
    cleaned = (raw or "document").strip().lower().replace("-", "_")
    try:
        return SuggestedAction(cleaned)
    except ValueError:
        return SuggestedAction.DOCUMENT


def calibrate_confidence(
    *,
    finding_ids: list[str],
    findings_by_id: dict[str, Finding],
    llm_confidence: float | None,
    unit_repos: set[str] | None = None,
) -> float:
    """Agreement-weighted confidence: mean finding scores × multi-goal / cross-repo bonus; LLM ±0.1."""
    selected = [findings_by_id[i] for i in finding_ids if i in findings_by_id]
    if not selected:
        base = 0.4
    else:
        mean = sum(f.score for f in selected) / len(selected)
        goals = {f.goal for f in selected}
        bonus = 1.1 if len(goals) >= 2 else 1.0
        base = min(0.99, mean * bonus)
    llm = float(llm_confidence) if llm_confidence is not None else base
    nudged = base + max(-0.1, min(0.1, llm - base))
    cross = False
    if selected:
        cross = any(bool(f.metrics.get("cross_repo")) for f in selected)
        if not cross and unit_repos is not None and len(unit_repos) >= 2:
            cross = True
        if not cross:
            for f in selected:
                if f.metadata.get("cross_repo"):
                    cross = True
                    break
    if cross:
        nudged = min(0.99, nudged * 1.12)
    if any(_is_architectural_wiring_finding(findings_by_id[i]) for i in finding_ids if i in findings_by_id):
        nudged = min(nudged, _WIRING_CONFIDENCE_CAP)
    return max(0.05, min(0.99, nudged))


def _estimate_loc_delta(action: str, unit_ids: list[str], units_by_id: dict[str, CodeUnit]) -> int:
    locs = []
    for uid in unit_ids:
        u = units_by_id.get(uid)
        if not u:
            continue
        locs.append(
            max(
                1,
                (u.span.end_line - u.span.start_line + 1)
                if u.span.end_line
                else u.source_snippet.count("\n") + 1,
            )
        )
    if not locs:
        return 0
    if action == SuggestedAction.DELETE_DUPLICATE.value and len(locs) >= 2:
        return min(locs)
    # Single-unit extract_shared is not a credible LOC win (e.g. placement noise)
    if action == SuggestedAction.EXTRACT_SHARED.value and len(unit_ids) >= 2:
        return max(0, min(locs) // 2)
    return 0


def plan_brief(
    brief: PlanningBrief,
    units_by_id: dict[str, CodeUnit],
    provider: LLMProvider,
) -> list[PlannerDecision]:
    findings = brief.findings
    findings_by_id = {f.id: f for f in findings}
    if len(findings) == 1:
        f = findings[0]
        row = PlannerDecision(
            title=f.title,
            action=f.action_hint,
            rationale=f.evidence,
            confidence=calibrate_confidence(
                finding_ids=[f.id],
                findings_by_id=findings_by_id,
                llm_confidence=f.score,
            ),
            finding_ids=[f.id],
            unit_ids=list(f.unit_ids),
            valid=True,
            estimated_loc_delta=_estimate_loc_delta(
                f.action_hint.value, list(f.unit_ids), units_by_id
            ),
            goal=f.goal,
        )
        return [_apply_architectural_wiring_rules(row, findings_by_id)]

    flag_lines = []
    for f in findings:
        unit_bits = [
            f"{c.unit_id}|{c.kind}|{c.repo}/{c.path}::{c.qualified_name}|{c.description}"
            for c in brief.units
            if c.unit_id in f.unit_ids
        ]
        flag_lines.append(
            f"- id={f.id} goal={f.goal.value} sensor={f.sensor} "
            f"action_hint={f.action_hint.value} title={f.title} "
            f"evidence={f.evidence} hints={','.join(f.hints) or 'none'} "
            f"units={', '.join(unit_bits)}"
        )
    system = (
        "You plan refactor changes from a PlanningBrief of overlapping findings. "
        "Respond with JSON only: "
        '{"decisions":[{"title":"...","action":"ensure_tests|merge|move|extract_shared|split|'
        'delete_duplicate|document","rationale":"...","confidence":0.0,'
        '"finding_ids":["..."],"flag_ids":["..."],"unit_ids":["..."],"valid":true}]}. '
        "Resolve conflicts coherently (merge A↔B OR move A→C, not both). "
        "Every finding id in the brief MUST appear in at least one decision "
        "(dedicated per-finding decisions are fine when they do not conflict). "
        "All actions are allowed including move. Use hints as soft guidance only. "
        "Reject only when evidence is insufficient. Never emit empty unit_ids. "
        "When findings show nested_traversal (or nested loops) together with similarity/"
        "clone evidence, prefer extract_shared over a bare split. "
        f"Candidate actions: {[a.value for a in brief.candidate_actions]}. "
        f"Goals present: {list(brief.findings_by_goal.keys())}."
    )
    user = (
        f"Component {brief.component_id}\nUnits:\n"
        + "\n".join(f"- {c.unit_id} {c.kind} {c.repo}/{c.path}::{c.qualified_name}" for c in brief.units)
        + "\nFindings:\n"
        + "\n".join(flag_lines)
    )
    try:
        parsed = provider.chat_json(system, user)
    except Exception as exc:  # noqa: BLE001
        logger.warning("plan failed: %s", exc)
        return [
            PlannerDecision(
                title=f"Unresolved: {findings[0].title}",
                action=findings[0].action_hint,
                rationale=f"Planner error: {exc}",
                confidence=0.2,
                finding_ids=[f.id for f in findings],
                unit_ids=sorted({uid for f in findings for uid in f.unit_ids}),
                valid=False,
                goal=findings[0].goal,
            )
        ]
    decisions = parsed.get("decisions")
    if not isinstance(decisions, list) or not decisions:
        best = max(findings, key=lambda x: x.score)
        return [
            PlannerDecision(
                title=best.title,
                action=best.action_hint,
                rationale="Fallback: highest-score finding after empty plan.",
                confidence=calibrate_confidence(
                    finding_ids=[best.id],
                    findings_by_id=findings_by_id,
                    llm_confidence=best.score,
                ),
                finding_ids=[best.id],
                unit_ids=list(best.unit_ids),
                valid=True,
                estimated_loc_delta=_estimate_loc_delta(
                    best.action_hint.value, list(best.unit_ids), units_by_id
                ),
                goal=best.goal,
            )
        ]
    out: list[PlannerDecision] = []
    for row in decisions:
        if not isinstance(row, dict):
            continue
        finding_ids = [str(x) for x in (row.get("finding_ids") or row.get("flag_ids") or [])]
        unit_ids = [str(x) for x in row.get("unit_ids") or []]
        if not unit_ids:
            selected = [f for f in findings if not finding_ids or f.id in finding_ids] or findings
            unit_ids = sorted({uid for f in selected for uid in f.unit_ids})
        action = _decision_to_action(str(row.get("action") or "document"))
        conf = calibrate_confidence(
            finding_ids=finding_ids,
            findings_by_id=findings_by_id,
            llm_confidence=float(row.get("confidence") or 0.5),
        )
        goal = None
        for fid in finding_ids:
            if fid in findings_by_id:
                goal = findings_by_id[fid].goal
                break
        out.append(
            _apply_architectural_wiring_rules(
                PlannerDecision(
                    title=str(row.get("title") or "decision"),
                    action=action,
                    rationale=str(row.get("rationale") or ""),
                    confidence=conf,
                    finding_ids=finding_ids,
                    unit_ids=unit_ids,
                    valid=bool(row.get("valid", True)),
                    estimated_loc_delta=_estimate_loc_delta(action.value, unit_ids, units_by_id),
                    goal=goal,
                ),
                findings_by_id,
            )
        )
    # Cover findings the model omitted so giant/partial plans do not silently drop work
    covered = {fid for d in out for fid in d.finding_ids}
    for finding in findings:
        if finding.id in covered:
            continue
        # Prefer attaching to an existing valid decision that already targets these units
        attached = False
        for idx, decision in enumerate(out):
            if not decision.valid:
                continue
            if set(finding.unit_ids) and set(finding.unit_ids).issubset(set(decision.unit_ids)):
                merged_ids = list(dict.fromkeys([*decision.finding_ids, finding.id]))
                out[idx] = decision.model_copy(update={"finding_ids": merged_ids})
                covered.add(finding.id)
                attached = True
                break
        if attached:
            continue
        out.append(
            _apply_architectural_wiring_rules(
                PlannerDecision(
                    title=finding.title,
                    action=finding.action_hint,
                    rationale="Fallback: finding not selected in LLM plan; using sensor action_hint.",
                    confidence=calibrate_confidence(
                        finding_ids=[finding.id],
                        findings_by_id=findings_by_id,
                        llm_confidence=finding.score,
                    ),
                    finding_ids=[finding.id],
                    unit_ids=list(finding.unit_ids),
                    valid=True,
                    estimated_loc_delta=_estimate_loc_delta(
                        finding.action_hint.value, list(finding.unit_ids), units_by_id
                    ),
                    goal=finding.goal,
                ),
                findings_by_id,
            )
        )
    return out


def _is_root_unit(unit: CodeUnit | None) -> bool:
    if unit is None:
        return True
    if unit.kind in {UnitKind.REPO, UnitKind.MODULE}:
        path = (unit.path or "").replace("\\", "/").strip()
        if path in {"", ".", "/", unit.repo}:
            return True
    path = (unit.path or "").replace("\\", "/").strip()
    return path in {"", ".", "/", unit.repo}


def _remap_document_targets(
    targets: list[ChangesetTarget],
    decision: PlannerDecision,
    units_by_id: dict[str, CodeUnit],
    findings_by_id: dict[str, Finding],
) -> list[ChangesetTarget] | None:
    """Drop or remap repo-root document targets using finding metadata.path."""
    if not targets:
        return None
    units = [units_by_id.get(t.unit_id) for t in targets if t.unit_id]
    if not units or not all(_is_root_unit(u) for u in units):
        return targets
    for fid in decision.finding_ids or []:
        finding = findings_by_id.get(fid)
        if finding is None or not isinstance(finding.metadata, dict):
            continue
        rel = str(finding.metadata.get("path") or "").replace("\\", "/").strip()
        repo = str(finding.metadata.get("repo") or (units[0].repo if units[0] else ""))
        if not rel or rel in {"", ".", "/", repo}:
            continue
        for u in units_by_id.values():
            if u.repo != repo:
                continue
            if _is_root_unit(u):
                continue
            up = (u.path or "").replace("\\", "/").lstrip("./")
            if up == rel or up.endswith("/" + rel) or rel.endswith(up):
                return [
                    ChangesetTarget(
                        repo=u.repo,
                        path=u.path,
                        symbol=u.qualified_name,
                        unit_id=u.id,
                    )
                ]
    return None


def decisions_to_changesets(
    decisions: list[PlannerDecision],
    units_by_id: dict[str, CodeUnit],
    findings_by_id: dict[str, Finding] | None = None,
) -> list[Changeset]:
    known = set(units_by_id)
    findings_by_id = findings_by_id or {}
    changesets: list[Changeset] = []
    for decision in decisions:
        payload = decision.model_dump(mode="json")
        # compat key for structural check
        payload["flag_ids"] = payload.get("finding_ids") or []
        if not decision.valid:
            continue
        reason = decision_structural_reason(payload, known, units_by_id)
        if reason:
            continue
        targets = []
        for uid in dict.fromkeys(decision.unit_ids):
            unit = units_by_id.get(uid)
            if not unit:
                continue
            targets.append(
                ChangesetTarget(
                    repo=unit.repo,
                    path=unit.path,
                    symbol=unit.qualified_name,
                    unit_id=unit.id,
                )
            )
        if not targets:
            continue
        action = decision.action.value
        if action == "document":
            remapped = _remap_document_targets(targets, decision, units_by_id, findings_by_id)
            if remapped is None:
                continue
            targets = remapped
        if not targets:
            continue
        goal = decision.goal.value if decision.goal else None
        locs = []
        for t in targets:
            if t.unit_id and t.unit_id in units_by_id:
                u = units_by_id[t.unit_id]
                locs.append(
                    max(
                        1,
                        (u.span.end_line - u.span.start_line + 1)
                        if u.span.end_line
                        else u.source_snippet.count("\n") + 1,
                    )
                )
        loc_under_review = sum(locs) if locs else 0
        removable = decision.estimated_loc_delta
        if action == "delete_duplicate" and len(locs) >= 2:
            removable = min(locs)
        elif action == "extract_shared" and locs:
            removable = max(0, min(locs) // 2)
        elif action in {"split", "ensure_tests", "move", "document"}:
            removable = 0

        upside = {
            "delete_duplicate": f"**Expected upside:** remove ~**{removable} LOC**.\n\n",
            "split": f"**Expected upside:** split ~**{locs[0] if locs else 0} LOC** (LOC removed = 0).\n\n",
            "extract_shared": f"**Expected upside:** extract shared; ~**{removable} LOC** save.\n\n",
            "move": "**Expected upside:** improve cohesion via placement.\n\n",
            "ensure_tests": "**Expected upside:** raise coverage / safer refactors.\n\n",
        }.get(action, "**Expected upside:** structural.\n\n")

        brief = (
            f"## {decision.title}\n\n"
            f"Action: `{action}`\nConfidence: {decision.confidence:.2f}\n"
            + (f"Goal: `{goal}`\n" if goal else "")
            + f"\n{upside}{decision.rationale}\n\n### Targets\n"
            + "\n".join(f"- `{t.repo}/{t.path}` :: `{t.symbol}`" for t in targets)
            + "\n\nApply this change only. Do not expand scope."
        )
        metrics: dict = {
            "estimated_loc_removed": removable,
            "loc_under_review": loc_under_review,
            "target_locs": locs,
            "goal": goal,
            "cross_repo": len({t.repo for t in targets}) >= 2,
        }
        for fid in decision.finding_ids or []:
            finding = findings_by_id.get(fid)
            if not finding:
                continue
            if finding.metrics:
                for key in ("cosine", "shape_similarity", "source_similarity", "name_similarity"):
                    if key in finding.metrics and key not in metrics:
                        metrics[key] = finding.metrics[key]
            if (
                "kind" not in metrics
                and isinstance(finding.metadata, dict)
                and finding.metadata.get("kind")
            ):
                metrics["kind"] = finding.metadata["kind"]
        wiring_kind = str(metrics.get("kind") or "")
        if wiring_kind in _ARCHITECTURAL_WIRING_KINDS and action == "ensure_tests":
            method_hint = ""
            for finding in (findings_by_id.get(fid) for fid in (decision.finding_ids or [])):
                if finding and isinstance(finding.metadata, dict):
                    method_hint = str(finding.metadata.get("method") or "")
                    if method_hint:
                        break
            title_line = decision.title
            brief = (
                f"## {title_line}\n\n"
                f"Action: `{action}`\nConfidence: {decision.confidence:.2f}\n\n"
                "**Integration brief:** characterize the wired handler with "
                "`@EmbeddedKafka` or Testcontainers before wire-vs-remove decisions.\n\n"
                f"{_WIRING_DECISION_NOTE}\n\n"
                f"{decision.rationale}\n\n### Targets\n"
                + "\n".join(f"- `{t.repo}/{t.path}` :: `{t.symbol}`" for t in targets)
                + "\n\nAsk the user before applying (HITL)."
            )
            if method_hint:
                metrics["method"] = method_hint
        changesets.append(
            Changeset(
                id=content_hash(f"cs:{decision.title}:{action}:{','.join(decision.unit_ids)}")[:16],
                title=decision.title,
                action=action,
                brief=brief,
                rationale=decision.rationale,
                confidence=decision.confidence,
                targets=targets,
                parallel_group=f"g-{action}",
                evidence_refs=list(decision.finding_ids),
                metrics_delta=metrics,
            )
        )
    return changesets


def plan_findings(
    findings: list[Finding],
    units: list[CodeUnit],
    provider: LLMProvider,
    *,
    concurrency: int = 8,
    max_component_size: int = 8,
) -> tuple[list[ConfirmedFinding], list[Changeset], list[PlannerDecision], UnitAgenda]:
    """Agenda-first planning — persist all structurally valid decisions (no top-K drop)."""
    units_by_id = {u.id: u for u in units}
    agenda = build_unit_agenda(findings)
    components = split_oversized_components(
        conflict_components(findings, units_by_id),
        max_size=max_component_size,
    )
    confirmed: list[ConfirmedFinding] = []
    decisions: list[PlannerDecision] = []

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
    ) as progress:
        task = progress.add_task("Planning agenda components", total=len(components))
        with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
            futures = {}
            for i, comp in enumerate(components):
                brief = build_planning_brief(comp, units_by_id, component_id=f"c{i}")
                futures[pool.submit(plan_brief, brief, units_by_id, provider)] = comp
            for fut in as_completed(futures):
                comp = futures[fut]
                try:
                    part = fut.result()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("component plan crashed: %s", exc)
                    part = []
                if not part:
                    # Never leave a whole component uncovered
                    part = [
                        PlannerDecision(
                            title=f.title,
                            action=f.action_hint,
                            rationale="Fallback: empty/crashed component plan.",
                            confidence=calibrate_confidence(
                                finding_ids=[f.id],
                                findings_by_id={x.id: x for x in comp},
                                llm_confidence=f.score,
                            ),
                            finding_ids=[f.id],
                            unit_ids=list(f.unit_ids),
                            valid=True,
                            estimated_loc_delta=_estimate_loc_delta(
                                f.action_hint.value, list(f.unit_ids), units_by_id
                            ),
                            goal=f.goal,
                        )
                        for f in comp
                    ]
                known = set(units_by_id)
                comp_by_id = {x.id: x for x in comp}
                cleaned: list[PlannerDecision] = []
                for row in part:
                    row = _apply_architectural_wiring_rules(row, comp_by_id)
                    payload = row.model_dump(mode="json")
                    payload["flag_ids"] = payload.get("finding_ids") or []
                    reason = (
                        decision_structural_reason(payload, known, units_by_id)
                        if row.valid
                        else "invalid"
                    )
                    if reason and row.valid:
                        row = row.model_copy(
                            update={
                                "valid": False,
                                "rationale": f"{row.rationale} [structural:{reason}]",
                            }
                        )
                    cleaned.append(row)
                decisions.extend(cleaned)
                by_id = {f.id: f for f in comp}
                seen_in_comp: dict[str, ConfirmedFinding] = {}
                for row in cleaned:
                    for fid in row.finding_ids:
                        finding = by_id.get(fid)
                        if not finding:
                            continue
                        entry = ConfirmedFinding(
                            finding=finding,
                            valid=row.valid,
                            reasoning=row.rationale,
                            refined_action=row.action if row.valid else None,
                        )
                        prev = seen_in_comp.get(fid)
                        # Prefer a valid selection if the model emitted duplicates
                        if prev is None or (entry.valid and not prev.valid):
                            seen_in_comp[fid] = entry
                confirmed.extend(seen_in_comp.values())
                progress.advance(task)

    seen = {c.finding.id for c in confirmed}
    for finding in findings:
        if finding.id not in seen:
            confirmed.append(
                ConfirmedFinding(
                    finding=finding,
                    valid=False,
                    reasoning="Dropped during plan (not selected).",
                )
            )

    changesets = decisions_to_changesets(
        decisions, units_by_id, findings_by_id={f.id: f for f in findings}
    )
    changesets = _dedupe_changesets(changesets)
    # Prefer cross-repo + high confidence + real dedup upside in the agent queue
    changesets.sort(
        key=lambda c: (
            -int(bool(c.metrics_delta.get("cross_repo"))),
            -int(
                str(c.metrics_delta.get("goal") or "")
                in {"slop", "structural_clone", "method_similarity"}
            ),
            -float(c.confidence),
            -int(c.metrics_delta.get("estimated_loc_removed") or 0),
            -int(c.metrics_delta.get("loc_under_review") or 0),
        )
    )
    return confirmed, changesets, decisions, agenda


_CLONE_EXTRACT_GOALS = frozenset({"structural_clone", "method_similarity", "slop"})
_ROLE_SUFFIX = re.compile(
    r"(Support|Handler|Controller|Service|Helper|Base|Mixin|Trait)$"
)
_FRAMEWORK_ROLE_TOKENS = frozenset(
    {"Support", "Handler", "Controller", "Service", "Grpc", "Api", "Base"}
)


def _is_clone_extract(cs: Changeset) -> bool:
    if (cs.action or "").strip().lower() != "extract_shared":
        return False
    goal = str(cs.metrics_delta.get("goal") or "")
    if goal in _CLONE_EXTRACT_GOALS:
        return True
    for ref in cs.evidence_refs or []:
        low = ref.lower()
        if low.startswith("fp:") or "slop:cluster" in low or "sim." in low:
            return True
    return False


def _target_keys(cs: Changeset) -> set[tuple[str, str, str]]:
    return {
        (t.repo or "", t.path or "", t.symbol or "")
        for t in cs.targets or []
    }


def _target_paths(cs: Changeset) -> set[tuple[str, str]]:
    return {(t.repo or "", t.path or "") for t in cs.targets or []}


def _target_unit_ids(cs: Changeset) -> set[str]:
    return {t.unit_id for t in cs.targets or [] if t.unit_id}


def _package_prefix(symbol: str | None) -> str:
    if not symbol or "." not in symbol:
        return ""
    return symbol.rsplit(".", 1)[0]


def _simple_symbol(symbol: str | None) -> str:
    return (symbol or "").rsplit(".", 1)[-1]


def _framework_role_symbol(symbol: str | None) -> bool:
    simple = _simple_symbol(symbol)
    if _ROLE_SUFFIX.search(simple):
        return True
    return any(tok in simple for tok in _FRAMEWORK_ROLE_TOKENS)


def _extract_shared_clustered(a: Changeset, b: Changeset) -> bool:
    if not (_is_clone_extract(a) and _is_clone_extract(b)):
        return False
    refs_a = set(a.evidence_refs or [])
    refs_b = set(b.evidence_refs or [])
    if refs_a & refs_b:
        return True
    if _target_unit_ids(a) & _target_unit_ids(b):
        return True
    if _target_paths(a) & _target_paths(b):
        return True
    for ta in a.targets or []:
        for tb in b.targets or []:
            if ta.repo != tb.repo:
                continue
            pa, pb = _package_prefix(ta.symbol), _package_prefix(tb.symbol)
            if not pa or pa != pb:
                continue
            if _framework_role_symbol(ta.symbol) and _framework_role_symbol(tb.symbol):
                return True
    return False


def _fold_extract_shared(winner: Changeset, others: list[Changeset]) -> Changeset:
    targets = list(winner.targets or [])
    seen = _target_keys(winner)
    evidence = list(winner.evidence_refs or [])
    folded_ids = list(winner.metrics_delta.get("folded_changeset_ids") or [])

    for other in others:
        folded_ids.append(other.id)
        for ref in other.evidence_refs or []:
            if ref not in evidence:
                evidence.append(ref)
        for target in other.targets or []:
            key = (target.repo or "", target.path or "", target.symbol or "")
            if key in seen:
                continue
            seen.add(key)
            targets.append(target)

    metrics = dict(winner.metrics_delta or {})
    metrics["folded_changeset_ids"] = folded_ids
    metrics["primary_extract"] = True

    brief = winner.brief
    if "### Targets" in brief:
        prefix = brief.split("### Targets", 1)[0]
        target_lines = "\n".join(
            f"- `{t.repo}/{t.path}` :: `{t.symbol}`" for t in targets
        )
        brief = (
            f"{prefix}### Targets\n"
            f"{target_lines}\n\nApply this change only. Do not expand scope."
        )

    return winner.model_copy(
        update={
            "targets": targets,
            "evidence_refs": evidence,
            "metrics_delta": metrics,
            "brief": brief,
        }
    )


def _consolidate_clone_extracts(changesets: list[Changeset]) -> list[Changeset]:
    """One primary extract_shared per structural_clone/fingerprint cluster."""
    indices = [i for i, cs in enumerate(changesets) if _is_clone_extract(cs)]
    if len(indices) < 2:
        return changesets

    parent = list(range(len(indices)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for i in range(len(indices)):
        for j in range(i + 1, len(indices)):
            a = changesets[indices[i]]
            b = changesets[indices[j]]
            if _extract_shared_clustered(a, b):
                union(i, j)

    clusters: dict[int, list[int]] = {}
    for i in range(len(indices)):
        clusters.setdefault(find(i), []).append(indices[i])

    to_remove: set[str] = set()
    updates: dict[str, Changeset] = {}
    for members in clusters.values():
        if len(members) < 2:
            continue
        grouped = [changesets[i] for i in members]
        winner = max(grouped, key=lambda c: (c.confidence, len(c.targets), c.id))
        folded = [c for c in grouped if c.id != winner.id]
        updates[winner.id] = _fold_extract_shared(winner, folded)
        for cs in folded:
            to_remove.add(cs.id)

    out: list[Changeset] = []
    for cs in changesets:
        if cs.id in to_remove:
            continue
        out.append(updates.get(cs.id, cs))
    return out


def _is_self_merge_changeset(cs: Changeset) -> bool:
    """Drop merge/delete_duplicate when targets collapse to one repo+path+symbol."""
    action = (cs.action or "").strip().lower()
    if action not in {"merge", "delete_duplicate"}:
        return False
    if len(cs.targets or []) < 2:
        return False
    keys = {
        (
            t.repo or "",
            (t.path or "").replace("\\", "/").lstrip("./"),
            t.symbol or "",
        )
        for t in cs.targets
    }
    return len(keys) <= 1


def _dedupe_changesets(changesets: list[Changeset]) -> list[Changeset]:
    """Collapse near-identical agent tasks (same action + target set)."""
    best: dict[tuple, Changeset] = {}
    for cs in changesets:
        if _is_self_merge_changeset(cs):
            continue
        key = (
            cs.action,
            tuple(sorted((t.repo, t.path, t.symbol or "") for t in cs.targets)),
        )
        prev = best.get(key)
        if prev is None or cs.confidence > prev.confidence:
            best[key] = cs
    items = list(best.values())
    return _consolidate_clone_extracts(items)
