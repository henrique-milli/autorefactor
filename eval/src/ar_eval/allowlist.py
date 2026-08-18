"""Path allowlist for alpha self-improve edits."""

from __future__ import annotations

import re
from pathlib import Path

# Relative prefixes under the product repo root.
ALLOWED_PREFIXES: tuple[str, ...] = (
    "src/autorefactor/",
    "eval/personas/",
    "eval/schemas/",
    "docs/",
    "tests/",
)

FORBIDDEN_NAMES: frozenset[str] = frozenset(
    {
        ".env",
        "secrets.local.env",
        "corpus.local.yaml",
    }
)

KIND_RANK = {
    "friction": 0,
    "false_positive": 1,
    "bad_confidence": 2,
    "brief_quality": 3,
    "miss": 4,
    "logic_flaw": 5,
    "missing_feature": 6,
    "other": 7,
}

SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def normalize_repo_path(path: str) -> str:
    p = path.replace("\\", "/").lstrip("./")
    return p


def is_allowed_alpha_path(path: str) -> bool:
    rel = normalize_repo_path(path)
    name = Path(rel).name
    if name in FORBIDDEN_NAMES:
        return False
    if any(part in FORBIDDEN_NAMES for part in Path(rel).parts):
        return False
    if rel.startswith("eval/subjects/"):
        return False
    return any(rel == pref.rstrip("/") or rel.startswith(pref) for pref in ALLOWED_PREFIXES)


def filter_allowed_changes(changes: list[dict]) -> tuple[list[dict], list[str]]:
    """Return (kept, rejected_paths)."""
    kept: list[dict] = []
    rejected: list[str] = []
    for ch in changes:
        path = str((ch or {}).get("path") or "")
        if is_allowed_alpha_path(path):
            kept.append(ch)
        else:
            rejected.append(path)
    return kept, rejected


def rank_and_budget_changes(
    changes: list[dict],
    *,
    max_changes: int = 3,
    finding_meta: dict[str, dict] | None = None,
) -> list[dict]:
    """Keep at most ``max_changes`` allowlisted items, ranked by linked finding severity/kind."""
    kept, _ = filter_allowed_changes(changes)
    meta = finding_meta or {}

    def sort_key(ch: dict) -> tuple:
        fid = str(ch.get("finding_id") or "").lower()
        if not fid:
            m = re.search(r"\bf\d+\b", str(ch.get("rationale") or ""), re.I)
            fid = m.group(0).lower() if m else ""
        info = meta.get(fid) or {}
        sev = SEVERITY_RANK.get(str(info.get("severity") or "medium").lower(), 2)
        kind = KIND_RANK.get(str(info.get("kind") or "other").lower(), 7)
        return (sev, kind, str(ch.get("path") or ""))

    return sorted(kept, key=sort_key)[: max(0, max_changes)]
