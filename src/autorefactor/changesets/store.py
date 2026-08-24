from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from autorefactor.changesets.models import Changeset, ChangesetStatus, NextChangesetResult
from autorefactor.changesets.ranking import coverage_first_mode, prep_rank

_HITL_AT_THRESHOLD_ACTIONS = frozenset({"delete_duplicate", "merge"})
_THRESHOLD_BAND_ACTIONS = frozenset({"split", "move", "extract_shared"})
_ARCHITECTURAL_WIRING_KINDS = frozenset({"orphan_kafka", "test_only_handler"})
_THRESHOLD_BAND_WIDTH = 0.05
_BEHAVIOR_SENSITIVE_MARKERS = (
    "exceptionhandler",
    "@exceptionhandler",
    "handler merge",
    "slop dedup",
    "slop:",
)
_PERSISTENCE_DEP = re.compile(
    r"\b\w*(?:Repository|DAO|Datastore|Mapper|Client)\b",
    re.IGNORECASE,
)
_DATA_DTO_TARGET = re.compile(
    r"\b\w*(?:Data|DTO|Dto)\b|/(?:data|dto)/",
    re.IGNORECASE,
)
_MAPPER_ANNOTATION = re.compile(r"@Mapper\b")
_MAPPING_ANNOTATION = re.compile(r"@Mapping\b")
_WIRING_HANDLER_FROM_TITLE = re.compile(r"handler `(\w+)\.(\w+)`", re.IGNORECASE)
_WIRING_HANDLER_FROM_EVIDENCE = re.compile(r"`(\w+)\.(\w+)`")
_DEFAULT_AUTORUN_THRESHOLD = 0.6
_AUTORUN_BLOCKED_KINDS = frozenset(
    {"committed_secret", "disabled_startup_validation", "draft_in_memory_map"}
)


def _wiring_handler_names(changeset: Changeset) -> tuple[str | None, str | None]:
    title = changeset.title or ""
    blob = " ".join(filter(None, [title, changeset.rationale or "", changeset.brief or ""]))
    match = _WIRING_HANDLER_FROM_TITLE.search(title)
    if match:
        return match.group(1), match.group(2)
    match = _WIRING_HANDLER_FROM_EVIDENCE.search(blob)
    if match:
        return match.group(1), match.group(2)
    return None, None


def _handler_symbols_from_changeset(changeset: Changeset) -> set[str]:
    symbols: set[str] = set()
    class_name, method_name = _wiring_handler_names(changeset)
    if method_name:
        symbols.add(method_name)
        if class_name:
            symbols.add(f"{class_name}.{method_name}")
    meta_method = str((changeset.metrics_delta or {}).get("method") or "")
    if meta_method:
        symbols.add(meta_method)
    for target in changeset.targets or []:
        sym = target.symbol or ""
        if not sym:
            continue
        symbols.add(sym)
        symbols.add(sym.rsplit(".", 1)[-1])
    return symbols


def _structural_overlaps_wiring(structural: Changeset, wiring: Changeset) -> bool:
    wiring_syms = _handler_symbols_from_changeset(wiring)
    if not wiring_syms:
        return False
    for target in structural.targets or []:
        sym = target.symbol or ""
        simple = sym.rsplit(".", 1)[-1]
        if simple in wiring_syms or sym in wiring_syms:
            return True
        for ws in wiring_syms:
            if sym.endswith("." + ws) or ws in sym:
                return True
    return False


def unwired_handler_structural_hitl(
    changeset: Changeset,
    queue: list[Changeset],
) -> bool:
    """Split/extract_shared on handlers flagged by wiring siblings cannot auto_proceed."""
    action = (changeset.action or "").strip().lower()
    if action not in {"split", "extract_shared"}:
        return False
    if changeset.metrics_delta.get("followup"):
        return False
    wiring = [
        c
        for c in queue
        if c.id != changeset.id
        and str((c.metrics_delta or {}).get("kind") or "") in _ARCHITECTURAL_WIRING_KINDS
        and not c.metrics_delta.get("followup")
    ]
    return any(_structural_overlaps_wiring(changeset, w) for w in wiring)


def _changeset_text_blob(changeset: Changeset) -> str:
    parts = [
        changeset.title or "",
        changeset.rationale or "",
        changeset.brief or "",
        " ".join(changeset.evidence_refs or []),
    ]
    for target in changeset.targets or []:
        parts.extend([target.path or "", target.symbol or ""])
    return " ".join(parts)


def behavior_sensitive_dedup(changeset: Changeset) -> bool:
    """True for merge/delete_duplicate items that need HITL at the confidence band."""
    action = (changeset.action or "").strip().lower()
    if action not in _HITL_AT_THRESHOLD_ACTIONS:
        return False
    blob = _changeset_text_blob(changeset).lower()
    return any(marker in blob for marker in _BEHAVIOR_SENSITIVE_MARKERS)


def placement_risk_move(changeset: Changeset) -> bool:
    """Move into data/DTO types that inject persistence/repository dependencies."""
    if (changeset.action or "").strip().lower() != "move":
        return False
    blob = _changeset_text_blob(changeset)
    if not _PERSISTENCE_DEP.search(blob):
        return False
    return bool(_DATA_DTO_TARGET.search(blob))


def _low_confidence_hitl(changeset: Changeset, min_confidence: float) -> bool:
    action = (changeset.action or "").strip().lower()
    if action in _HITL_AT_THRESHOLD_ACTIONS:
        return changeset.confidence < min_confidence
    return changeset.confidence < min_confidence


def _threshold_band_hitl(changeset: Changeset, min_confidence: float) -> bool:
    action = (changeset.action or "").strip().lower()
    if action not in _THRESHOLD_BAND_ACTIONS:
        return False
    band_hi = min_confidence + _THRESHOLD_BAND_WIDTH
    return min_confidence < changeset.confidence <= band_hi


def _behavior_sensitive_band_hitl(changeset: Changeset, min_confidence: float) -> bool:
    if not behavior_sensitive_dedup(changeset):
        return False
    band_hi = min_confidence + _THRESHOLD_BAND_WIDTH
    if changeset.confidence < min_confidence:
        return False
    return changeset.confidence <= band_hi


def security_sensitive_hitl(changeset: Changeset) -> bool:
    """Committed secrets/credentials must never auto_proceed."""
    kind = str((changeset.metrics_delta or {}).get("kind") or "").lower()
    if kind == "committed_secret":
        return True
    blob = _changeset_text_blob(changeset).lower()
    if "committed_secret" in blob or "committed credential" in blob:
        return True
    if "mongodb+srv://" in blob or ("password=" in blob and "mongodb" in blob):
        return True
    return False


def slop_kind_hitl(changeset: Changeset) -> bool:
    """Behavioral/slop kinds must never auto_proceed on numeric confidence alone."""
    kind = str((changeset.metrics_delta or {}).get("kind") or "").lower()
    if kind in _AUTORUN_BLOCKED_KINDS - {"committed_secret"}:
        return True
    blob = _changeset_text_blob(changeset).lower()
    return any(token in blob for token in ("disabled_startup_validation", "draft_in_memory_map"))


def _target_identity_key(target) -> tuple[str, str, str]:  # noqa: ANN001
    return (
        target.repo or "",
        (target.path or "").replace("\\", "/").lstrip("./"),
        target.symbol or "",
    )


def is_self_merge_changeset(changeset: Changeset) -> bool:
    """True when merge/delete_duplicate targets collapse to one repo+path+symbol."""
    action = (changeset.action or "").strip().lower()
    if action not in _HITL_AT_THRESHOLD_ACTIONS:
        return False
    if len(changeset.targets or []) < 2:
        return False
    keys = {_target_identity_key(t) for t in changeset.targets}
    return len(keys) <= 1


def self_merge_hitl(changeset: Changeset) -> bool:
    """Overload self-merge pairs must never auto_proceed."""
    return is_self_merge_changeset(changeset)


def _ensure_tests_dep_ids(changeset: Changeset, queue: list[Changeset]) -> set[str]:
    by_id = {c.id: c for c in queue}
    out: set[str] = set()
    for dep_id in changeset.depends_on or []:
        dep = by_id.get(dep_id)
        if dep and (dep.action or "").strip().lower() == "ensure_tests":
            out.add(dep_id)
    return out


def uncovered_structural_hitl(
    changeset: Changeset,
    queue: list[Changeset] | None = None,
) -> bool:
    """Structural primaries on uncovered production units need HITL without ensure_tests prep."""
    if (changeset.metrics_delta or {}).get("followup"):
        return False
    action = (changeset.action or "").strip().lower()
    if action not in {"split", "extract_shared", "move"}:
        return False
    metrics = changeset.metrics_delta or {}
    if not metrics.get("needs_coverage"):
        return False
    if queue and _ensure_tests_dep_ids(changeset, queue):
        return False
    return True


def blast_radius_hitl(changeset: Changeset) -> bool:
    """Multi-target extract_shared or cross-class delete_duplicate needs HITL."""
    action = (changeset.action or "").strip().lower()
    targets = changeset.targets or []
    if action == "extract_shared" and len(targets) >= 6:
        return True
    if action == "delete_duplicate" and len(targets) >= 2:
        classes = {
            (t.symbol or "").rsplit(".", 1)[0]
            for t in targets
            if (t.symbol or "").strip()
        }
        classes.discard("")
        if len(classes) >= 2:
            return True
    return False


def ensure_tests_padding_hitl(
    changeset: Changeset,
    min_confidence: float = _DEFAULT_AUTORUN_THRESHOLD,
) -> bool:
    """Low-confidence ensure_tests still HITL; high-confidence prep autoruns."""
    if (changeset.action or "").strip().lower() != "ensure_tests":
        return False
    return float(changeset.confidence) < float(min_confidence)


def architectural_wiring_hitl(changeset: Changeset) -> bool:
    """Orphan Kafka / test-only handlers need wire-vs-remove decisions — never auto_proceed."""
    kind = str((changeset.metrics_delta or {}).get("kind") or "")
    return kind in _ARCHITECTURAL_WIRING_KINDS


def framework_sensitive_split(changeset: Changeset) -> bool:
    """MapStruct @Mapper/@Mapping interface splits need HITL — partial extract breaks compile-time codegen."""
    action = (changeset.action or "").strip().lower()
    if action != "split":
        return False
    blob = _changeset_text_blob(changeset)
    return bool(_MAPPER_ANNOTATION.search(blob) or _MAPPING_ANNOTATION.search(blob))


def apply_confidence_gates(
    changeset: Changeset,
    *,
    min_confidence: float = _DEFAULT_AUTORUN_THRESHOLD,
) -> Changeset:
    """Cap risky framework-proxy splits below autorun threshold at queue write time."""
    from autorefactor.changesets.ranking import is_threshold_band_structure_split

    updated = changeset
    if framework_sensitive_split(changeset):
        cap = max(0.05, min_confidence - 0.01)
        if changeset.confidence <= cap:
            updated = changeset
        else:
            note = (
                "\n\n**MapStruct note:** partial interface splits risk compile-time mapper "
                "generation — review `uses` / `@Context` constraints before applying."
            )
            brief = changeset.brief if note.strip() in changeset.brief else changeset.brief + note
            metrics = dict(changeset.metrics_delta or {})
            metrics["confidence_capped"] = True
            metrics["original_confidence"] = changeset.confidence
            metrics["framework_sensitive_split"] = True
            updated = changeset.model_copy(
                update={"confidence": cap, "brief": brief, "metrics_delta": metrics}
            )

    if is_threshold_band_structure_split(updated) and _threshold_band_hitl(updated, min_confidence):
        metrics = dict(updated.metrics_delta or {})
        extras: list[str] = []
        cosine = metrics.get("cosine")
        shape = metrics.get("shape_similarity")
        if cosine is not None:
            extras.append(f"embedding cosine={float(cosine):.3f}")
        if shape is not None:
            extras.append(f"shape similarity={float(shape):.3f}")
        if not extras:
            blob = _changeset_text_blob(updated)
            for key, pattern in (
                ("cosine", r"cosine=([\d.]+)"),
                ("shape", r"shape=([\d.]+)"),
            ):
                match = re.search(pattern, blob, re.IGNORECASE)
                if match:
                    extras.append(f"embedding {key}={float(match.group(1)):.3f}")
        if extras:
            suffix = f"\n\n**Similarity evidence:** {', '.join(extras)}."
            if suffix.strip() not in (updated.brief or ""):
                updated = updated.model_copy(update={"brief": (updated.brief or "") + suffix})
    return updated


def changeset_needs_hitl(
    changeset: Changeset,
    min_confidence: float,
    *,
    deps_satisfied: bool = True,
    queue: list[Changeset] | None = None,
) -> tuple[bool, str | None]:
    """Shared autorun vs HITL gate used by next_pending and queue counts.

    Unsatisfied deps are waiting (``blocked_deps``), not a user confirmation.
    High-confidence items auto-proceed unless a real judgment kind applies.
    UI-approved items (``user_decision=proceed``) skip HITL.
    """
    if not deps_satisfied:
        return False, "blocked_deps"
    decision = (changeset.user_decision or "").strip().lower()
    if decision == "proceed":
        return False, None
    action = (changeset.action or "").strip().lower()
    # Coverage prep is gated once at the prep-phase CTA — never per-item HITL.
    if action == "ensure_tests":
        return False, None
    if security_sensitive_hitl(changeset):
        return True, "security_sensitive"
    if slop_kind_hitl(changeset):
        return True, "slop_sensitive"
    if self_merge_hitl(changeset):
        return True, "self_merge"
    if architectural_wiring_hitl(changeset):
        return True, "architectural_wiring"
    if queue and unwired_handler_structural_hitl(changeset, queue):
        return True, "wiring_blocked_structural"
    if framework_sensitive_split(changeset):
        return True, "framework_sensitive_split"
    if placement_risk_move(changeset):
        return True, "placement_risk"
    plan_disp = str((changeset.metrics_delta or {}).get("plan_disposition") or "").strip().lower()
    if plan_disp == "review":
        return True, "plan_review"
    # Planner keep: do not re-open as low_confidence FP dumpster.
    if plan_disp == "keep":
        if _threshold_band_hitl(changeset, min_confidence):
            return True, "threshold_band"
        if _behavior_sensitive_band_hitl(changeset, min_confidence):
            return True, "behavior_sensitive_dedup"
        if action in _HITL_AT_THRESHOLD_ACTIONS and changeset.confidence == min_confidence:
            return True, "low_confidence"
        return False, None
    if _low_confidence_hitl(changeset, min_confidence):
        return True, "low_confidence"
    if _threshold_band_hitl(changeset, min_confidence):
        return True, "threshold_band"
    if _behavior_sensitive_band_hitl(changeset, min_confidence):
        return True, "behavior_sensitive_dedup"
    if action in _HITL_AT_THRESHOLD_ACTIONS and changeset.confidence == min_confidence:
        return True, "low_confidence"
    return False, None


_VALID_USER_DECISIONS = frozenset({"proceed", "skip", "blocked"})


def set_user_decision(
    out_dir: Path,
    changeset_id: str,
    decision: str | None,
) -> Changeset:
    """Persist a UI review decision (decide now, agent executes later).

    ``proceed`` — keep pending, agent auto_proceeds.
    ``skip`` / ``blocked`` — mark status accordingly.
    ``None`` — undo approve (clear decision; restore pending if was skipped/blocked by UI).
    """
    from autorefactor.queue import load_queue, save_queue

    items = load_queue(out_dir)
    found: Changeset | None = None
    idx = -1
    for i, item in enumerate(items):
        if item.id == changeset_id:
            found = item
            idx = i
            break
    if found is None:
        raise KeyError(f"changeset not found: {changeset_id}")

    if decision is None or decision == "":
        updates: dict[str, Any] = {"user_decision": None}
        # Undo: if UI skip/blocked left a terminal status, reopen for review.
        if found.user_decision in {"skip", "blocked"} and found.status in {
            ChangesetStatus.SKIPPED,
            ChangesetStatus.BLOCKED,
        }:
            updates["status"] = ChangesetStatus.PENDING
            if (found.notes or "").startswith("UI review:"):
                updates["notes"] = ""
        updated = found.model_copy(update=updates)
    else:
        norm = str(decision).strip().lower()
        if norm not in _VALID_USER_DECISIONS:
            raise ValueError(f"invalid decision: {decision}")
        if norm == "proceed":
            updated = found.model_copy(
                update={
                    "user_decision": "proceed",
                    "status": ChangesetStatus.PENDING
                    if found.status
                    in {ChangesetStatus.SKIPPED, ChangesetStatus.BLOCKED}
                    else found.status,
                    "notes": "UI review: approved — agent should apply when executing queue",
                }
            )
        elif norm == "skip":
            updated = found.model_copy(
                update={
                    "user_decision": "skip",
                    "status": ChangesetStatus.SKIPPED,
                    "notes": "UI review: skipped",
                }
            )
        else:
            updated = found.model_copy(
                update={
                    "user_decision": "blocked",
                    "status": ChangesetStatus.BLOCKED,
                    "notes": "UI review: blocked",
                }
            )
    items[idx] = updated
    save_queue(out_dir, items)
    return updated


def hitl_questions_for_reason(
    changeset: Changeset,
    min_confidence: float,
    reason: str,
    *,
    unmet_deps: list[str] | None = None,
) -> list[str]:
    if reason == "blocked_deps":
        deps = ", ".join(unmet_deps or [])
        return [
            f"Waiting: dependencies not done yet: {deps}.",
            f"'{changeset.title}' ({changeset.action}) cannot apply until prep is complete.",
            "This is not a confirmation prompt — drain or skip the blocking prep item first.",
        ]
    if reason == "ensure_tests":
        return [
            "ensure_tests prep is decided once at the prep-phase coverage CTA (yes/no).",
            "Do not ask proceed/skip/blocked on individual ensure_tests items.",
        ]
    if reason == "security_sensitive":
        return [
            "Security-sensitive finding (committed secret/credential) — never auto_proceed.",
            f"Proceed with '{changeset.title}' ({changeset.action}) only after confirming "
            "secret rotation and externalization via env/vault.",
            "Reply yes to continue, skip to skip, or blocked.",
        ]
    if reason == "slop_sensitive":
        return [
            "Behavioral/slop finding (disabled validation, draft in-memory map, etc.) — "
            "never auto_proceed on numeric confidence alone.",
            f"Proceed with '{changeset.title}' ({changeset.action}) only after confirming "
            "the fix matches the rationale (enable/remove/replace, not a blind split).",
            "Reply yes to continue, skip to skip, or blocked.",
        ]
    if reason == "self_merge":
        return [
            "Self-merge: merge/delete_duplicate targets share the same repo+path+symbol — "
            "likely an overload false positive.",
            f"Proceed with '{changeset.title}' ({changeset.action})?",
            "Reply yes to continue, skip to skip, or blocked.",
        ]
    if reason == "blast_radius":
        return [
            "High blast-radius structural change (multi-target extract_shared or "
            "cross-class delete_duplicate) — confirm before applying.",
            f"Proceed with '{changeset.title}' ({changeset.action})?",
            "Reply yes to continue, skip to skip, or blocked.",
        ]
    if reason == "uncovered_structural":
        return [
            "Structural change on production units without Jacoco coverage prep — "
            "confirm or run ensure_tests first.",
            f"Proceed with '{changeset.title}' ({changeset.action})?",
            "Reply yes to continue, skip to skip, or blocked.",
        ]
    if reason == "architectural_wiring":
        return [
            "Architectural Kafka wiring — choose wire_kafka_consumer or remove_dead_wiring "
            "(never auto-apply extract_shared on a test-only handler).",
            f"How should '{changeset.title}' ({changeset.action}) be resolved?",
            "Reply wire_kafka_consumer, remove_dead_wiring, skip, or blocked.",
        ]
    if reason == "wiring_blocked_structural":
        return [
            "Handler split/extract_shared blocked until Kafka wiring decision completes — "
            "wire_kafka_consumer or remove_dead_wiring must be resolved first.",
            f"Proceed with '{changeset.title}' ({changeset.action}) anyway?",
            "Reply yes to continue, skip to skip, or resolve wiring first.",
        ]
    if reason == "placement_risk":
        return [
            "Placement move injects persistence/repository dependencies into a data/DTO type — "
            "confirm before applying.",
            f"Proceed with '{changeset.title}' ({changeset.action})?",
            "Reply yes to continue, skip to skip, or provide guidance.",
        ]
    if reason == "plan_review":
        return [
            "Planner marked this for human review (uncertain keep vs trash / high blast radius).",
            f"Proceed with '{changeset.title}' ({changeset.action})?",
            "Reply yes to continue, skip to skip, or blocked.",
        ]
    if reason == "framework_sensitive_split":
        return [
            "MapStruct @Mapper/@Mapping interface split — partial helper extraction can break "
            "compile-time mapper generation; confirm `uses` / `@Context` wiring before applying.",
            f"Proceed with '{changeset.title}' ({changeset.action})?",
            "Reply yes to continue, skip to skip, or provide guidance.",
        ]
    if reason == "threshold_band":
        band_hi = min_confidence + _THRESHOLD_BAND_WIDTH
        return [
            (
                f"Confidence {changeset.confidence:.2f} is in the review band "
                f"({min_confidence:.2f}, {band_hi:.2f}] — confirm before auto-applying."
            ),
            f"Proceed with '{changeset.title}' ({changeset.action})?",
            "Reply yes to continue, skip to skip, or provide guidance.",
        ]
    if reason == "behavior_sensitive_dedup":
        return [
            "Behavior-sensitive merge/dedup requires human confirmation at this confidence band.",
            f"Proceed with '{changeset.title}' ({changeset.action})?",
            "Reply yes to continue, skip to skip, or provide guidance.",
        ]
    return [
        f"Confidence {changeset.confidence:.2f} is at or below threshold {min_confidence:.2f}.",
        f"Proceed with '{changeset.title}' ({changeset.action})?",
        "Reply yes to continue, skip to skip, or provide guidance.",
    ]


class ChangesetStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> list[Changeset]:
        if not self.path.exists():
            return []
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        if isinstance(raw, list):
            items = raw
        else:
            items = raw.get("changesets", [])
        return [Changeset.model_validate(item) for item in items]

    def save(self, changesets: list[Changeset]) -> None:
        gated = [apply_confidence_gates(c) for c in changesets]
        payload = {
            "changesets": [c.model_dump(mode="json") for c in gated],
        }
        self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def list(self) -> list[Changeset]:
        return self.load()

    def get(self, changeset_id: str) -> Changeset | None:
        for item in self.load():
            if item.id == changeset_id:
                return item
        return None

    def mark(self, changeset_id: str, status: ChangesetStatus | str, notes: str = "") -> Changeset:
        items = self.load()
        status_enum = ChangesetStatus(status) if isinstance(status, str) else status
        for idx, item in enumerate(items):
            if item.id == changeset_id:
                updated = item.model_copy(
                    update={"status": status_enum, "notes": notes or item.notes}
                )
                items[idx] = updated
                self.save(items)
                return updated
        raise KeyError(f"changeset not found: {changeset_id}")

    def _sort_pending_key(self, c: Changeset) -> tuple:
        items = self.load()
        cov_first = coverage_first_mode(items)
        ui_approved = 1 if (c.user_decision or "").strip().lower() == "proceed" else 0
        return (
            -(1 if c.status == ChangesetStatus.IN_PROGRESS else 0),
            -ui_approved,  # UI-approved items drain before unreviewed HITL
            prep_rank(c, coverage_first=cov_first),
            -int(bool(c.metrics_delta.get("cross_repo"))),
            -c.confidence,
            -int(c.metrics_delta.get("estimated_loc_removed") or 0),
        )

    @staticmethod
    def _deps_satisfied(changeset: Changeset, ids_done: set[str]) -> bool:
        return all(dep in ids_done for dep in (changeset.depends_on or []))

    def next_pending(self, *, min_confidence: float = 0.0) -> NextChangesetResult:
        pending = [
            c
            for c in self.load()
            if c.status in {ChangesetStatus.PENDING, ChangesetStatus.IN_PROGRESS}
        ]
        if not pending:
            return NextChangesetResult(done=True)
        # While any UI-approved proceed items remain, execute only those (+ in_progress).
        ui_approved = [
            c
            for c in pending
            if (c.user_decision or "").strip().lower() == "proceed"
        ]
        if ui_approved:
            pending = [
                c
                for c in pending
                if (c.user_decision or "").strip().lower() == "proceed"
                or c.status == ChangesetStatus.IN_PROGRESS
            ]
        # Only DONE dependencies unblock dependents (SKIPPED/BLOCKED do not).
        ids_done = {
            c.id for c in self.load() if c.status == ChangesetStatus.DONE
        }
        ready = [c for c in pending if self._deps_satisfied(c, ids_done)]
        if not ready:
            blocked = [c for c in pending if not self._deps_satisfied(c, ids_done)]
            blocked.sort(key=self._sort_pending_key)
            chosen = blocked[0]
            unmet = [d for d in (chosen.depends_on or []) if d not in ids_done]
            return NextChangesetResult(
                changeset=chosen,
                needs_confirmation=False,
                hitl_reason="blocked_deps",
                questions=hitl_questions_for_reason(
                    chosen, min_confidence, "blocked_deps", unmet_deps=unmet
                ),
            )
        ready.sort(key=self._sort_pending_key)
        chosen = ready[0]
        all_items = self.load()
        needs, reason = changeset_needs_hitl(
            chosen, min_confidence, deps_satisfied=True, queue=all_items
        )
        if needs and reason:
            return NextChangesetResult(
                changeset=chosen,
                needs_confirmation=True,
                hitl_reason=reason,
                questions=hitl_questions_for_reason(chosen, min_confidence, reason),
            )
        return NextChangesetResult(changeset=chosen)
