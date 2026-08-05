from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn

from coderazor.models import CodeUnit, ConfirmedFinding, Flag, SuggestedAction
from coderazor.providers.base import LLMProvider

logger = logging.getLogger(__name__)


def _parse_action(value: str | None, fallback: SuggestedAction) -> SuggestedAction:
    if not value:
        return fallback
    try:
        return SuggestedAction(value.strip().lower())
    except ValueError:
        return fallback


def confirm_flags(
    flags: list[Flag],
    units: list[CodeUnit],
    provider: LLMProvider,
    *,
    concurrency: int = 8,
) -> list[ConfirmedFinding]:
    by_id = {u.id: u for u in units}

    def _confirm_one(flag: Flag) -> ConfirmedFinding:
        unit_blurbs = []
        for uid in flag.unit_ids[:4]:
            unit = by_id.get(uid)
            if not unit:
                continue
            unit_blurbs.append(
                f"- {unit.repo}:{unit.path}::{unit.qualified_name}\n"
                f"  desc: {unit.describe_text()[:300]}\n"
                f"  snippet:\n{unit.source_snippet[:800]}"
            )
        system = (
            "You validate refactoring flags. Respond with JSON only: "
            '{"valid": true|false, "reasoning": "...", "action": '
            '"merge|move|extract_shared|split|delete_duplicate|document"}. '
            "Reject false positives such as trivial getters, generated code, or unrelated similar names."
        )
        user = (
            f"title: {flag.title}\n"
            f"source: {flag.source.value}\n"
            f"evidence: {flag.evidence}\n"
            f"suggested_action: {flag.suggested_action.value}\n\n"
            f"units:\n" + "\n".join(unit_blurbs)
        )
        parsed = provider.chat_json(system, user)
        valid = bool(parsed.get("valid", False))
        reasoning = str(parsed.get("reasoning", "")).strip()
        action = _parse_action(str(parsed.get("action", "")), flag.suggested_action)
        return ConfirmedFinding(
            flag=flag,
            valid=valid,
            reasoning=reasoning or ("accepted" if valid else "rejected"),
            refined_action=action if valid else None,
        )

    findings: list[ConfirmedFinding] = []
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
    ) as progress:
        task = progress.add_task("Confirming flags", total=len(flags))
        with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
            futures = [pool.submit(_confirm_one, f) for f in flags]
            for fut in as_completed(futures):
                findings.append(fut.result())
                progress.advance(task)

    findings.sort(key=lambda f: (-f.flag.score, f.flag.title))
    return findings
