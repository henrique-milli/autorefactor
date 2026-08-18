"""Deterministic post-rebuild probes — fail when planner output did not move."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def _norm_path(path: str) -> str:
    return (path or "").replace("\\", "/").strip()


def _is_repo_root_path(path: str, repo: str | None = None) -> bool:
    p = _norm_path(path)
    return p in {"", ".", "/"} or (bool(repo) and p == repo)


def probe_queue_changesets(
    changesets: list[dict[str, Any]],
    *,
    expect_coverage_prep: bool = False,
) -> dict[str, Any]:
    """
    Assert Janus-shaped queue invariants after a product patch + rebuild.

    Returns ``{ok, failures, notes}``. Failures are human-readable strings.
    """
    failures: list[str] = []
    notes: list[str] = []

    docs = [c for c in changesets if (c.get("action") or "").lower() == "document"]
    root_docs = []
    file_docs = []
    for c in docs:
        targets = c.get("targets") or []
        if not targets:
            root_docs.append(c)
            continue
        if all(_is_repo_root_path(t.get("path") or "", t.get("repo")) for t in targets):
            root_docs.append(c)
        else:
            file_docs.append(c)

    if root_docs and file_docs:
        # Same draft_map / slop twin: root doc beside a file-scoped doc
        failures.append(
            f"repo-root document changesets present alongside file-scoped docs "
            f"({len(root_docs)} root, {len(file_docs)} file)"
        )
    elif root_docs:
        failures.append(f"repo-root document target(s) remain: {[c.get('id') for c in root_docs]}")

    padding_ensure = []
    ensure_n = 0
    for c in changesets:
        if (c.get("action") or "").lower() != "ensure_tests":
            continue
        ensure_n += 1
        for t in c.get("targets") or []:
            path = _norm_path(t.get("path") or "")
            symbol = str(t.get("symbol") or "")
            if "CoverageTest" in path or "CoverageTest" in symbol:
                padding_ensure.append(c.get("id"))
                break
    if expect_coverage_prep and ensure_n < 1:
        failures.append("coverage prep expected but ensure_tests count is 0")
    if padding_ensure:
        failures.append(
            f"ensure_tests still targets CoverageTest padding: {padding_ensure}"
        )

    placement_fp = []
    for c in changesets:
        if (c.get("action") or "").lower() != "move":
            continue
        title = str(c.get("title") or "")
        brief = str(c.get("brief") or "")
        blob = f"{title} {brief}"
        if "getBonusFromPamResponse" in blob and "BonusService" in blob:
            placement_fp.append(c.get("id"))
    if placement_fp:
        failures.append(
            f"placement move of getBonusFromPamResponse→BonusService still queued: {placement_fp}"
        )

    self_merges = []
    for c in changesets:
        action = (c.get("action") or "").lower()
        if action not in {"merge", "delete_duplicate"}:
            continue
        symbols = [str(t.get("symbol") or "") for t in (c.get("targets") or [])]
        symbols = [s for s in symbols if s]
        if len(symbols) >= 2 and len(set(symbols)) == 1:
            self_merges.append(c.get("id"))
    if self_merges:
        failures.append(f"self-merge identical symbol pairs still queued: {self_merges}")

    # Coverage-first: high-conf structural should appear before a long doc HITL streak
    ensure_present = any((c.get("action") or "").lower() == "ensure_tests" for c in changesets)
    if ensure_present:
        first_structural_idx = None
        first_doc_streak_start = None
        doc_streak = 0
        for i, c in enumerate(changesets):
            action = (c.get("action") or "").lower()
            conf = float(c.get("confidence") or 0)
            is_followup = bool((c.get("metrics_delta") or {}).get("followup"))
            if (
                action in {"split", "extract_shared", "delete_duplicate", "move"}
                and conf >= 0.85
                and not is_followup
                and first_structural_idx is None
            ):
                first_structural_idx = i
            if action == "document" and conf < 0.7:
                if first_doc_streak_start is None:
                    first_doc_streak_start = i
                doc_streak += 1
            elif first_doc_streak_start is not None and doc_streak < 5:
                first_doc_streak_start = None
                doc_streak = 0
        if (
            first_structural_idx is not None
            and first_doc_streak_start is not None
            and doc_streak >= 5
            and first_doc_streak_start < first_structural_idx
        ):
            failures.append(
                "coverage-first ordering failed: ≥5 low-conf document items "
                f"before high-conf structural (doc@{first_doc_streak_start}, "
                f"structural@{first_structural_idx})"
            )
        else:
            notes.append("coverage-first ordering ok or N/A")

    return {
        "ok": len(failures) == 0,
        "failures": failures,
        "notes": notes,
        "counts": {
            "changesets": len(changesets),
            "documents": len(docs),
            "root_documents": len(root_docs),
        },
    }


def spec_blob(spec: dict[str, Any] | None) -> str:
    if not isinstance(spec, dict):
        return ""
    parts = [str(spec.get("summary") or "")]
    for change in spec.get("changes") or []:
        if not isinstance(change, dict):
            continue
        parts.extend(
            [
                str(change.get("path") or ""),
                str(change.get("rationale") or ""),
                str(change.get("acceptance") or ""),
            ]
        )
    return " ".join(parts).lower()


def spec_expect_coverage_prep(spec: dict[str, Any] | None) -> bool:
    blob = spec_blob(spec)
    return any(
        token in blob
        for token in (
            "ensure_tests",
            "jacoco",
            "followups.py",
            "coverage prep",
            "prep ranked",
        )
    )


def spec_expect_security_hitl(spec: dict[str, Any] | None) -> bool:
    blob = spec_blob(spec)
    return any(
        token in blob
        for token in ("committed_secret", "security_sensitive", "credential")
    )


def spec_targets_planner(spec: dict[str, Any] | None) -> bool:
    blob = spec_blob(spec)
    return any(
        token in blob
        for token in (
            "planner.py",
            "followups.py",
            "ranking.py",
            "plan/",
            "similarity.py",
        )
    )


def probe_security_sensitive(changesets: list[dict[str, Any]]) -> list[str]:
    failures: list[str] = []
    for cs in changesets:
        if not isinstance(cs, dict):
            continue
        kind = str((cs.get("metrics_delta") or {}).get("kind") or "").lower()
        blob = " ".join(
            [
                str(cs.get("title") or ""),
                str(cs.get("rationale") or ""),
                str(cs.get("brief") or ""),
                kind,
            ]
        ).lower()
        if kind != "committed_secret" and "committed_secret" not in blob:
            continue
        cid = cs.get("id")
        if cs.get("auto_proceed") is True:
            failures.append(f"committed_secret {cid} still auto_proceed")
        if cs.get("needs_confirmation") is False:
            failures.append(f"committed_secret {cid} needs_confirmation=false")
        reason = str(cs.get("hitl_reason") or "")
        if cs.get("needs_confirmation") is True and reason and reason != "security_sensitive":
            failures.append(
                f"committed_secret {cid} hitl_reason={reason} (want security_sensitive)"
            )
    return failures


def apply_spec_probes(
    probe: dict[str, Any],
    changesets: list[dict[str, Any]],
    spec: dict[str, Any] | None,
    *,
    planner_unchanged: bool = False,
    sticky_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Fold alpha_spec expectations into a probe result."""
    failures = list(probe.get("failures") or [])
    notes = list(probe.get("notes") or [])
    ensure_n = sum(
        1
        for c in changesets
        if isinstance(c, dict) and str(c.get("action") or "").lower() == "ensure_tests"
    )
    notes.append(f"ensure_tests_count={ensure_n}")
    if spec_expect_coverage_prep(spec) and ensure_n < 1:
        msg = "coverage prep expected but ensure_tests count is 0"
        if msg not in failures:
            failures.append(msg)
    if spec_expect_security_hitl(spec):
        failures.extend(probe_security_sensitive(changesets))
    sticky = list(sticky_ids or [])
    if spec_targets_planner(spec) and (planner_unchanged or sticky):
        failures.append(f"spec targeted planner but queue sticky_ids={sticky}")
    probe = dict(probe)
    probe["failures"] = failures
    probe["notes"] = notes
    probe["ok"] = not failures
    probe["ensure_tests_count"] = ensure_n
    return probe


def sticky_changeset_ids(
    prior_feedback: dict[str, Any],
    changesets: list[dict[str, Any]],
) -> list[str]:
    """Prior finding evidence changeset_ids that still appear in the new queue."""
    prior_ids: set[str] = set()
    for f in prior_feedback.get("findings") or []:
        if not isinstance(f, dict):
            continue
        for ev in f.get("evidence") or []:
            if isinstance(ev, dict) and ev.get("changeset_id"):
                prior_ids.add(str(ev["changeset_id"]))
    if not prior_ids:
        return []
    current = {str(c.get("id")) for c in changesets if c.get("id")}
    return sorted(prior_ids & current)


def probe_from_queue_file(queue_path: Path) -> dict[str, Any]:
    """Load changesets.json (list or {changesets: [...]}) and probe."""
    import json

    if not queue_path.is_file():
        return {"ok": False, "failures": [f"missing queue file: {queue_path}"], "notes": []}
    try:
        data = json.loads(queue_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return {"ok": False, "failures": [f"invalid queue json: {exc}"], "notes": []}
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = data.get("changesets") or data.get("items") or []
    else:
        items = []
    if not isinstance(items, list):
        return {"ok": False, "failures": ["queue payload is not a list"], "notes": []}
    return probe_queue_changesets([c for c in items if isinstance(c, dict)])
