from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn

from coderazor.analyze.conflict_graph import conflict_components
from coderazor.changesets.models import Changeset, ChangesetTarget
from coderazor.models import (
    CodeUnit,
    ConfirmedFinding,
    Flag,
    SuggestedAction,
    content_hash,
)
from coderazor.providers.base import LLMProvider

logger = logging.getLogger(__name__)


def _decision_to_action(raw: str) -> str:
    cleaned = (raw or "document").strip().lower().replace("-", "_")
    allowed = {a.value for a in SuggestedAction}
    return cleaned if cleaned in allowed else SuggestedAction.DOCUMENT.value


def reconcile_component(
    flags: list[Flag],
    units_by_id: dict[str, CodeUnit],
    provider: LLMProvider,
) -> list[dict]:
    """Ask LLM to collapse a conflict component into non-overlapping decisions."""
    if len(flags) == 1:
        flag = flags[0]
        return [
            {
                "title": flag.title,
                "action": flag.suggested_action.value,
                "rationale": flag.evidence,
                "confidence": max(0.4, min(1.0, flag.score if flag.score else 0.55)),
                "flag_ids": [flag.id],
                "unit_ids": list(flag.unit_ids),
                "valid": True,
            }
        ]

    flag_lines = []
    for flag in flags:
        unit_bits = []
        for uid in flag.unit_ids[:6]:
            unit = units_by_id.get(uid)
            if unit:
                unit_bits.append(f"{unit.repo}/{unit.path}::{unit.qualified_name}")
        flag_lines.append(
            f"- id={flag.id} source={flag.source.value} action={flag.suggested_action.value} "
            f"title={flag.title} evidence={flag.evidence} units={'; '.join(unit_bits)}"
        )
    system = (
        "You reconcile conflicting code-refactor flags that touch the same symbols. "
        "Respond with JSON only: "
        '{"decisions":[{"title":"...","action":"merge|move|extract_shared|split|delete_duplicate|document",'
        '"rationale":"...","confidence":0.0,"flag_ids":["..."],"unit_ids":["..."],"valid":true}]}. '
        "Emit the minimal non-overlapping set of decisions. Reject noise with valid=false."
    )
    user = "Flags:\n" + "\n".join(flag_lines)
    try:
        parsed = provider.chat_json(system, user)
    except Exception as exc:  # noqa: BLE001
        logger.warning("reconcile failed for component: %s", exc)
        return [
            {
                "title": f"Unresolved: {flags[0].title}",
                "action": flags[0].suggested_action.value,
                "rationale": f"Reconcile error: {exc}",
                "confidence": 0.2,
                "flag_ids": [f.id for f in flags],
                "unit_ids": sorted({uid for f in flags for uid in f.unit_ids}),
                "valid": False,
            }
        ]
    decisions = parsed.get("decisions")
    if not isinstance(decisions, list) or not decisions:
        # Fallback: keep highest-score flag only
        best = max(flags, key=lambda f: f.score)
        return [
            {
                "title": best.title,
                "action": best.suggested_action.value,
                "rationale": "Fallback: kept highest-score flag after empty reconcile.",
                "confidence": max(0.3, best.score),
                "flag_ids": [best.id],
                "unit_ids": list(best.unit_ids),
                "valid": True,
            }
        ]
    out: list[dict] = []
    for row in decisions:
        if not isinstance(row, dict):
            continue
        flag_ids = [str(x) for x in row.get("flag_ids") or []]
        unit_ids = [str(x) for x in row.get("unit_ids") or []]
        if not unit_ids:
            selected = [f for f in flags if not flag_ids or f.id in flag_ids] or flags
            unit_ids = sorted({uid for f in selected for uid in f.unit_ids})
        out.append(
            {
                "title": str(row.get("title") or "decision"),
                "action": _decision_to_action(str(row.get("action") or "document")),
                "rationale": str(row.get("rationale") or ""),
                "confidence": float(row.get("confidence") or 0.5),
                "flag_ids": flag_ids,
                "unit_ids": unit_ids,
                "valid": bool(row.get("valid", True)),
            }
        )
    return out


def decisions_to_changesets(
    decisions: list[dict],
    units_by_id: dict[str, CodeUnit],
) -> list[Changeset]:
    changesets: list[Changeset] = []
    for idx, decision in enumerate(decisions):
        if not decision.get("valid", True):
            continue
        unit_ids = decision.get("unit_ids") or []
        targets = []
        for uid in unit_ids:
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
        action = decision.get("action") or "document"
        title = decision.get("title") or f"{action} change"
        rationale = decision.get("rationale") or ""
        confidence = float(decision.get("confidence") or 0.5)
        cid = content_hash(f"cs:{title}:{action}:{','.join(unit_ids)}")[:16]
        brief = (
            f"## {title}\n\n"
            f"Action: `{action}`\n"
            f"Confidence: {confidence:.2f}\n\n"
            f"{rationale}\n\n"
            "### Targets\n"
            + "\n".join(f"- `{t.repo}/{t.path}` :: `{t.symbol}`" for t in targets)
            + "\n\nApply this change only. Do not expand scope."
        )
        # Independent groups by action bucket index for crude parallelism
        changesets.append(
            Changeset(
                id=cid,
                title=title,
                action=action,
                brief=brief,
                rationale=rationale,
                confidence=confidence,
                targets=targets,
                parallel_group=f"g-{action}",
                evidence_refs=[str(x) for x in decision.get("flag_ids") or []],
                metrics_delta={"estimated_loc_removed": max(0, 10 * len(targets))},
            )
        )
    return changesets


def reconcile_flags(
    flags: list[Flag],
    units: list[CodeUnit],
    provider: LLMProvider,
    *,
    concurrency: int = 8,
) -> tuple[list[ConfirmedFinding], list[Changeset]]:
    units_by_id = {u.id: u for u in units}
    components = conflict_components(flags)
    findings: list[ConfirmedFinding] = []
    decisions: list[dict] = []

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
    ) as progress:
        task = progress.add_task("Reconciling flag components", total=len(components))
        with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
            futures = {
                pool.submit(reconcile_component, comp, units_by_id, provider): comp
                for comp in components
            }
            for fut in as_completed(futures):
                comp = futures[fut]
                try:
                    part = fut.result()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("component reconcile crashed: %s", exc)
                    part = []
                decisions.extend(part)
                # Mirror into ConfirmedFinding for compatibility
                by_id = {f.id: f for f in comp}
                for row in part:
                    for fid in row.get("flag_ids") or []:
                        flag = by_id.get(fid)
                        if not flag:
                            continue
                        findings.append(
                            ConfirmedFinding(
                                flag=flag,
                                valid=bool(row.get("valid", True)),
                                reasoning=str(row.get("rationale") or ""),
                                refined_action=(
                                    SuggestedAction(_decision_to_action(str(row.get("action"))))
                                    if row.get("valid", True)
                                    else None
                                ),
                            )
                        )
                progress.advance(task)

    # Ensure every flag appears once in findings
    seen = {f.flag.id for f in findings}
    for flag in flags:
        if flag.id not in seen:
            findings.append(
                ConfirmedFinding(
                    flag=flag,
                    valid=False,
                    reasoning="Dropped during reconcile (not selected).",
                )
            )

    changesets = decisions_to_changesets(decisions, units_by_id)
    return findings, changesets
