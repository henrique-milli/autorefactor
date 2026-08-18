"""Same-episode retest after product patches."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}


def _read_persona(eval_dir: Path, name: str) -> str:
    path = eval_dir / "personas" / name
    return path.read_text(encoding="utf-8")


def retest_prompt(
    *,
    eval_dir: Path,
    episode_id: str,
    engine_url: str,
    prior_feedback_path: Path,
    iteration: int,
) -> str:
    persona = _read_persona(eval_dir, "user_retest.md")
    return (
        f"{persona}\n\n"
        f"Episode id: `{episode_id}`.\n"
        f"Engine URL: `{engine_url}`.\n"
        f"Self-improve iteration just finished: {iteration}.\n"
        f"Prior user feedback checklist: `{prior_feedback_path}`.\n"
        "Analyze has been re-run with the **updated** product.\n"
        "Mission: score each prior finding id from MCP on this new analyze. "
        "Success is a `retest.json` checklist, not a subject rebuild.\n"
        "Write `retest.json` in the working directory when done.\n"
    )


def score_retest(retest: dict[str, Any], prior_findings: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute resolution score from retest.json vs prior finding ids."""
    results = retest.get("results") if isinstance(retest.get("results"), list) else []
    by_id = {
        str(r.get("id")): str(r.get("status") or "unknown").lower()
        for r in results
        if isinstance(r, dict) and r.get("id")
    }
    prior_ids = [str(f.get("id")) for f in prior_findings if isinstance(f, dict) and f.get("id")]
    if not prior_ids:
        return {"ok": True, "score": 1.0, "resolved": 0, "total": 0, "by_id": by_id}

    weights = {"resolved": 1.0, "partial": 0.5, "unresolved": 0.0, "unknown": 0.0}
    scored = 0.0
    resolved = 0
    for fid in prior_ids:
        st = by_id.get(fid, "unknown")
        scored += weights.get(st, 0.0)
        if st == "resolved":
            resolved += 1
    total = len(prior_ids)
    return {
        "ok": True,
        "score": scored / total,
        "resolved": resolved,
        "total": total,
        "by_id": by_id,
        "statuses": {fid: by_id.get(fid, "unknown") for fid in prior_ids},
    }


def load_prior_findings(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    if not isinstance(data, dict):
        return []
    findings = data.get("findings") or []
    return [f for f in findings if isinstance(f, dict)]
