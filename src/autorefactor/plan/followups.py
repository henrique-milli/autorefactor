from __future__ import annotations

import re
from pathlib import Path

from autorefactor.changesets.models import Changeset, ChangesetStatus, ChangesetTarget
from autorefactor.changesets.ranking import coverage_first_mode, prep_rank
from autorefactor.domain import (
    CodeUnit,
    CoverageSnapshot,
    DocumentGap,
    UnitKind,
    content_hash,
)
from autorefactor.document.detect import find_document_gaps
from autorefactor.testing.gaps import (
    is_coverage_padding_path,
    is_test_target_unit,
    index_production_units_by_repo,
    resolve_mapper_caller_units,
    resolve_production_units_for_ensure_tests,
    unit_has_characterization_test,
    _production_priority,
)
from autorefactor.testing.inventory import inventory_test_files

_FOLLOWUP_CONFIDENCE = 0.55
FOLLOWUP_CONFIDENCE = _FOLLOWUP_CONFIDENCE
_SLOP_PARENT_KINDS = frozenset({"disabled_startup_validation", "draft_in_memory_map"})
_HANDLER_REGULATOR = re.compile(r"(Handler|Mapper|Regulator|Service)", re.IGNORECASE)
_ARCHITECTURAL_WIRING_KINDS = frozenset({"orphan_kafka", "test_only_handler"})
_WIRING_HANDLER_FROM_TITLE = re.compile(r"handler `(\w+)\.(\w+)`", re.IGNORECASE)
_WIRING_HANDLER_FROM_EVIDENCE = re.compile(r"`(\w+)\.(\w+)`")
_PROCESS_HANDLER_METHOD = re.compile(r"^process\w*(?:Transaction|Message|Event)|^handle\w+", re.IGNORECASE)


def _coverage_is_estimated(coverage: CoverageSnapshot | None) -> bool:
    if coverage is None:
        return False
    return (coverage.source or "").lower() == "estimated"


def _coverage_index_empty(coverage: CoverageSnapshot | None) -> bool:
    if coverage is None:
        return True
    return not coverage.files


def _coverage_needs_tests(
    unit: CodeUnit,
    coverage: CoverageSnapshot | None,
    *,
    threshold: float,
    existing_tests: list[str] | None = None,
) -> bool:
    """Whether ensure_tests should target this unit.

    Measured % under threshold always needs work (even if *Test.java exists).
    Measured % at/above threshold does not. Without a measured match, an existing
    characterization suite short-circuits; unmatched files stay uncovered.
    """
    if coverage is None:
        return False
    if _coverage_is_estimated(coverage):
        return False
    if is_test_target_unit(unit):
        return False
    source = (coverage.source or "").lower()
    if not coverage.files and source in {"missing", "unknown", "", "estimated"}:
        return False
    if unit.repo in (coverage.repos_without_coverage or []) and not coverage.files:
        return False

    from autorefactor.coverage_paths import coverage_path_matches

    matched_pct: float | None = None
    for st in coverage.files or []:
        sp = (st.path or "").replace("\\", "/")
        if coverage_path_matches(unit.path, sp, repo=unit.repo):
            matched_pct = float(st.percent)
            break

    if matched_pct is not None:
        return matched_pct < threshold

    # No measured match for this unit.
    if existing_tests and unit_has_characterization_test(unit, existing_tests):
        return False
    if coverage.files:
        # File absent from report → treat as uncovered when reports exist
        return True
    if coverage.overall_percent is not None and float(coverage.overall_percent) < threshold:
        return True
    return False


def _is_draft_map_parent(cs: Changeset) -> bool:
    """True when parent is a draft in-memory map finding (hashed or literal refs)."""
    metrics = cs.metrics_delta or {}
    if str(metrics.get("kind") or "") == "draft_in_memory_map":
        return True
    refs = cs.evidence_refs or []
    if any(str(r).startswith("slop:draft_map") for r in refs):
        return True
    blob = f"{cs.title or ''} {cs.rationale or ''} {cs.brief or ''}".lower()
    if "draft in-memory mapping" in blob:
        return True
    if "constructor-initialized hashmap" in blob and "draft" in blob:
        return True
    return False


def _coverage_goal_pressure(
    changesets: list[Changeset],
    *,
    coverage: CoverageSnapshot | None = None,
    coverage_finding_count: int = 0,
) -> bool:
    """True when measured coverage, estimated Jacoco tooling, or explicit prep should drive ensure_tests."""
    index_empty = _coverage_index_empty(coverage)
    if coverage_finding_count > 0:
        return True
    if _coverage_is_estimated(coverage):
        return True
    primary_splits = sum(
        1
        for cs in changesets
        if (cs.action or "").strip().lower() == "split"
        and not (cs.metrics_delta or {}).get("followup")
    )
    primary_tests = sum(
        1
        for cs in changesets
        if (cs.action or "").strip().lower() == "ensure_tests"
        and not (cs.metrics_delta or {}).get("followup")
    )
    if primary_splits and primary_tests == 0:
        return True
    for cs in changesets:
        action = (cs.action or "").strip().lower()
        metrics = cs.metrics_delta or {}
        if action == "ensure_tests" and not metrics.get("followup"):
            return True
        if _is_draft_map_parent(cs):
            return True
        goal = str(metrics.get("goal") or "").lower()
        kind = str(metrics.get("kind") or "").lower()
        if goal in {"coverage", "test_quality"}:
            return True
        if kind in {"coverage_padding", "test_quality", "draft_in_memory_map"}:
            return True
    if not index_empty:
        return False
    if primary_splits:
        return True
    return False


def _remap_primary_ensure_tests(
    changesets: list[Changeset],
    units: list[CodeUnit],
    *,
    coverage: CoverageSnapshot | None,
    existing_tests: list[str] | None,
    coverage_threshold: float,
    coverage_pressure: bool = False,
) -> list[Changeset]:
    """Rewrite or drop primary ensure_tests that target padding / test paths."""
    units_by_id = {u.id: u for u in units}
    production_by_repo = index_production_units_by_repo(units)
    out: list[Changeset] = []
    for cs in changesets:
        action = (cs.action or "").strip().lower()
        if action != "ensure_tests" or (cs.metrics_delta or {}).get("followup"):
            out.append(cs)
            continue

        draft = _is_draft_map_parent(cs)
        if _coverage_is_estimated(coverage) and not draft and not coverage_pressure:
            # Estimated coverage without goal pressure: drop Jacoco-driven padding primaries.
            continue

        target_units = [
            units_by_id[t.unit_id]
            for t in cs.targets
            if t.unit_id and t.unit_id in units_by_id
        ]
        if not target_units:
            out.append(cs)
            continue

        evidence = " ".join(
            filter(None, [cs.rationale or "", cs.brief or "", cs.title or ""])
        )
        prep_kind = str((cs.metrics_delta or {}).get("kind") or "")
        remapped = resolve_production_units_for_ensure_tests(
            target_units,
            units,
            coverage=coverage,
            evidence_text=evidence,
            prep_kind=prep_kind,
            production_by_repo=production_by_repo,
        )
        if draft:
            for u in target_units:
                if u.kind == UnitKind.TYPE:
                    remapped.extend(resolve_mapper_caller_units(u, units))

        seen: set[str] = set()
        final: list[CodeUnit] = []
        for u in remapped:
            if u.id in seen or is_test_target_unit(u):
                continue
            if existing_tests and unit_has_characterization_test(u, existing_tests):
                # Low measured coverage still needs ensure even when *Test.java exists.
                if not _coverage_needs_tests(
                    u,
                    coverage,
                    threshold=coverage_threshold,
                    existing_tests=existing_tests,
                ):
                    if not draft:
                        continue
            seen.add(u.id)
            final.append(u)

        if not final:
            continue

        metrics = dict(cs.metrics_delta or {})
        metrics["needs_coverage"] = True
        metrics["coverage_gap_count"] = len(final)
        padding_origin = any(is_test_target_unit(u) for u in target_units)
        original_kind = str(metrics.get("kind") or "")
        if padding_origin or original_kind == "coverage_padding":
            if not draft:
                metrics["kind"] = "remapped_from_padding"
            title = _ensure_tests_title_for_units(final)
            rationale = _ensure_tests_rationale_for_units(
                final,
                threshold=coverage_threshold,
                remapped_from_padding=True,
            )
        else:
            title = cs.title
            rationale = cs.rationale
        if draft:
            metrics["kind"] = "draft_in_memory_map"
        out.append(
            cs.model_copy(
                update={
                    "targets": _targets_for_units(final),
                    "title": title,
                    "rationale": rationale,
                    "brief": _ensure_tests_brief(
                        final,
                        title=title,
                        threshold=coverage_threshold,
                        coverage_index_empty=_coverage_index_empty(coverage),
                        coverage_estimated=_coverage_is_estimated(coverage),
                    ),
                    "metrics_delta": metrics,
                }
            )
        )
    return out


def _keep_prep_unit_despite_existing_test(
    unit: CodeUnit,
    existing_tests: list[str] | None,
    *,
    coverage: CoverageSnapshot | None,
    index_empty: bool,
    structural_parent: bool,
    target_unit_ids: set[str],
) -> bool:
    """When the coverage index is empty, padding/mock suites must not suppress split prep."""
    if not existing_tests or not unit_has_characterization_test(unit, existing_tests):
        return True
    if not index_empty:
        return False
    if structural_parent and unit.id in target_unit_ids:
        return True
    if _HANDLER_REGULATOR.search(unit.qualified_name or ""):
        return True
    from autorefactor.testing.gaps import suggested_test_path

    suggested = suggested_test_path(unit)
    if is_coverage_padding_path(suggested):
        return True
    repo = unit.repo
    stem = Path(unit.path).stem.lower()
    class_sym = unit.qualified_name.rsplit(".", 1)[-1].lower()
    for test_path in existing_tests:
        if repo and not test_path.startswith(f"{repo}/"):
            continue
        if is_coverage_padding_path(test_path):
            continue
        test_stem = Path(test_path.replace("\\", "/")).stem.lower()
        if stem in test_stem or class_sym in test_stem:
            return False
    return True


def _resolve_ensure_test_units(
    target_units: list[CodeUnit],
    all_units: list[CodeUnit],
    *,
    coverage: CoverageSnapshot | None,
    parent: Changeset,
    existing_tests: list[str] | None,
    coverage_threshold: float,
    coverage_pressure: bool = False,
    production_by_repo: dict[str, list[CodeUnit]] | None = None,
) -> list[CodeUnit]:
    """Production-only ensure_tests targets; exclude padding/test paths."""
    cov_units: list[CodeUnit] = []
    draft = _is_draft_map_parent(parent)
    if coverage and not _coverage_is_estimated(coverage):
        raw = [
            u
            for u in target_units
            if _coverage_needs_tests(
                u,
                coverage,
                threshold=coverage_threshold,
                existing_tests=existing_tests,
            )
        ]
        evidence = " ".join(
            filter(None, [parent.rationale or "", parent.brief or "", parent.title or ""])
        )
        seeds = raw if raw else (target_units if coverage_pressure else [])
        if seeds:
            cov_units = resolve_production_units_for_ensure_tests(
                seeds,
                all_units,
                coverage=coverage,
                evidence_text=evidence,
                prep_kind=str((parent.metrics_delta or {}).get("kind") or ""),
                production_by_repo=production_by_repo,
            )
    elif coverage_pressure and not _coverage_is_estimated(coverage):
        evidence = " ".join(
            filter(None, [parent.rationale or "", parent.brief or "", parent.title or ""])
        )
        cov_units = resolve_production_units_for_ensure_tests(
            target_units,
            all_units,
            coverage=coverage,
            evidence_text=evidence,
            prep_kind=str((parent.metrics_delta or {}).get("kind") or ""),
            production_by_repo=production_by_repo,
        )
    elif _coverage_is_estimated(coverage) and (draft or coverage_pressure):
        evidence = " ".join(
            filter(None, [parent.rationale or "", parent.brief or "", parent.title or ""])
        )
        cov_units = resolve_production_units_for_ensure_tests(
            target_units,
            all_units,
            coverage=coverage,
            evidence_text=evidence,
            prep_kind=str((parent.metrics_delta or {}).get("kind") or ""),
            production_by_repo=production_by_repo,
        )

    # Draft-map silent-skip: always emit caller ensure_tests (even when coverage estimated).
    if draft:
        for u in target_units:
            if u.kind == UnitKind.TYPE:
                cov_units.extend(resolve_mapper_caller_units(u, all_units))

    index_empty = _coverage_index_empty(coverage)
    parent_action = (parent.action or "").strip().lower()
    structural_parent = parent_action in {"split", "extract_shared", "move"}
    target_unit_ids = {u.id for u in target_units}
    if not cov_units and index_empty and structural_parent and coverage_pressure:
        cov_units = resolve_production_units_for_ensure_tests(
            target_units,
            all_units,
            coverage=coverage,
            evidence_text=" ".join(
                filter(None, [parent.rationale or "", parent.brief or "", parent.title or ""])
            ),
            prep_kind=str((parent.metrics_delta or {}).get("kind") or ""),
            production_by_repo=production_by_repo,
        )

    seen: set[str] = set()
    out: list[CodeUnit] = []
    for u in cov_units:
        if u.id in seen or is_test_target_unit(u):
            continue
        if _coverage_needs_tests(
            u,
            coverage,
            threshold=coverage_threshold,
            existing_tests=existing_tests,
        ):
            pass
        elif existing_tests and unit_has_characterization_test(u, existing_tests):
            if not _keep_prep_unit_despite_existing_test(
                u,
                existing_tests,
                coverage=coverage,
                index_empty=index_empty,
                structural_parent=structural_parent,
                target_unit_ids=target_unit_ids,
            ):
                continue
        elif coverage is not None and coverage.files and not draft:
            # Measured OK or otherwise not needing tests — drop.
            continue
        seen.add(u.id)
        out.append(u)
    return out


def _ensure_tests_unit_names(units: list[CodeUnit], *, limit: int = 3) -> list[str]:
    return [u.qualified_name.rsplit(".", 1)[-1] for u in units[:limit]]


def _ensure_tests_title_for_units(units: list[CodeUnit]) -> str:
    names = _ensure_tests_unit_names(units)
    if not names:
        return "Ensure tests for production units"
    if len(units) == 1:
        return f"Ensure tests for {names[0]}"
    if len(units) <= 3:
        return f"Ensure tests for {', '.join(names)}"
    extra = len(units) - 3
    return f"Ensure tests for {', '.join(names)} (+{extra} more)"


def _ensure_tests_rationale_for_units(
    units: list[CodeUnit],
    *,
    threshold: float,
    remapped_from_padding: bool = False,
) -> str:
    names = ", ".join(_ensure_tests_unit_names(units))
    out = (
        f"{len(units)} production target(s) need characterization coverage "
        f"for {names} (below {threshold:.0f}%). "
        "Do not expand Jacoco padding / symbol-exists-only tests."
    )
    if remapped_from_padding:
        out += " Remapped from coverage-padding test target to production units."
    return out


def _ensure_tests_brief(
    units: list[CodeUnit],
    *,
    title: str,
    threshold: float,
    coverage_index_empty: bool = False,
    coverage_estimated: bool = False,
    integration_wiring: bool = False,
) -> str:
    target_lines = "\n".join(
        f"- `{u.repo}/{u.path}` :: `{u.qualified_name}` ({u.kind.value})"
        for u in units
    )
    matrix_note = ""
    if integration_wiring:
        matrix_note = (
            "\n**Integration brief:** characterize the wired handler with "
            "`@EmbeddedKafka` or Testcontainers — document-only prep does not "
            "substitute for Kafka consumer coverage.\n"
        )
    elif coverage_index_empty or any(
        _HANDLER_REGULATOR.search(u.qualified_name) for u in units
    ):
        matrix_note = (
            "\n**Message-matrix brief:** characterize handler/regulator branches and "
            "distinct return constants (e.g. RegulatorMessage variants) before structural splits.\n"
        )
    index_note = ""
    if coverage_index_empty:
        if coverage_estimated:
            index_note = (
                "\n**Estimated-only coverage** — Jacoco index empty; prep from production "
                "targets and agent friction debt, not padding suites.\n"
            )
        else:
            index_note = (
                "\n**Jacoco index empty** — prep from production targets and agent friction debt, "
                "not padding suites.\n"
            )
    return (
        f"## {title}\n\n"
        f"Action: `ensure_tests`\n"
        f"Write or expand characterization tests for the listed targets until "
        f"Jacoco-relevant behavior is covered (threshold {threshold:.0f}%). "
        f"A test class existing is not enough — raise measured coverage on the production units.\n\n"
        "**Do not expand Jacoco padding / symbol-exists-only tests.**"
        f"{index_note}{matrix_note}\n\n"
        f"### Resolved targets\n{target_lines or '(no production units resolved)'}\n\n"
        "Ask the user before applying (HITL)."
    )


def _targets_for_units(units: list[CodeUnit]) -> list[ChangesetTarget]:
    return [
        ChangesetTarget(
            repo=u.repo,
            path=u.path,
            symbol=u.qualified_name,
            unit_id=u.id,
        )
        for u in units
    ]


def _wiring_handler_names(parent: Changeset) -> tuple[str | None, str | None]:
    title = parent.title or ""
    blob = " ".join(filter(None, [title, parent.rationale or "", parent.brief or ""]))
    match = _WIRING_HANDLER_FROM_TITLE.search(title)
    if match:
        return match.group(1), match.group(2)
    match = _WIRING_HANDLER_FROM_EVIDENCE.search(blob)
    if match:
        return match.group(1), match.group(2)
    return None, None


def _resolve_wiring_integration_units(
    parent: Changeset,
    target_units: list[CodeUnit],
    all_units: list[CodeUnit],
) -> list[CodeUnit]:
    """Handler methods for orphan Kafka / test-only wiring — not Application.main."""
    kind = str((parent.metrics_delta or {}).get("kind") or "")
    if kind not in _ARCHITECTURAL_WIRING_KINDS:
        return []
    class_name, method_name = _wiring_handler_names(parent)
    repos = {u.repo for u in target_units} or {u.repo for u in all_units}
    target_ids = {u.id for u in target_units}
    target_type_names = {
        u.qualified_name.rsplit(".", 1)[-1]
        for u in target_units
        if u.kind == UnitKind.TYPE
    }
    for u in target_units:
        if u.kind == UnitKind.FUNCTION:
            target_type_names.add(u.qualified_name.rsplit(".", 1)[0].rsplit(".", 1)[-1])

    candidates: list[tuple[float, CodeUnit]] = []
    for u in all_units:
        if u.repo not in repos or u.kind != UnitKind.FUNCTION:
            continue
        if is_test_target_unit(u):
            continue
        simple_method = u.qualified_name.rsplit(".", 1)[-1]
        if simple_method == "main":
            continue
        parent_class = u.qualified_name.rsplit(".", 1)[0].rsplit(".", 1)[-1]
        if "Application" in parent_class and not _PROCESS_HANDLER_METHOD.match(simple_method):
            continue
        matched = False
        if method_name and simple_method == method_name:
            matched = True
        elif class_name and parent_class == class_name and (
            not method_name or simple_method == method_name
        ):
            matched = method_name is not None or bool(_PROCESS_HANDLER_METHOD.match(simple_method))
        elif u.parent_id in target_ids or parent_class in target_type_names:
            matched = bool(_PROCESS_HANDLER_METHOD.match(simple_method))
        if not matched:
            continue
        candidates.append((_production_priority(u, None), u))

    candidates.sort(key=lambda t: (-t[0], t[1].qualified_name))
    seen: set[str] = set()
    out: list[CodeUnit] = []
    for _, u in candidates:
        if u.id in seen:
            continue
        seen.add(u.id)
        out.append(u)
        if len(out) >= 3:
            break
    return out


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


def _wire_structural_wiring_deps(changesets: list[Changeset]) -> list[Changeset]:
    """Split/extract_shared on unwired handlers depends_on the wiring decision item."""
    wiring = [
        c
        for c in changesets
        if str((c.metrics_delta or {}).get("kind") or "") in _ARCHITECTURAL_WIRING_KINDS
        and not c.metrics_delta.get("followup")
    ]
    if not wiring:
        return changesets
    out: list[Changeset] = []
    for cs in changesets:
        action = (cs.action or "").strip().lower()
        if action not in {"split", "extract_shared"} or cs.metrics_delta.get("followup"):
            out.append(cs)
            continue
        deps = list(cs.depends_on or [])
        for w in wiring:
            if w.id == cs.id:
                continue
            if _structural_overlaps_wiring(cs, w) and w.id not in deps:
                deps.append(w.id)
        if deps != (cs.depends_on or []):
            out.append(cs.model_copy(update={"depends_on": deps}))
        else:
            out.append(cs)
    return out


def _remap_wiring_primary_targets(
    changesets: list[Changeset],
    units: list[CodeUnit],
) -> list[Changeset]:
    """Wiring primaries target handler methods, not repo-root TYPE anchors."""
    units_by_id = {u.id: u for u in units}
    out: list[Changeset] = []
    for cs in changesets:
        kind = str((cs.metrics_delta or {}).get("kind") or "")
        if (
            kind not in _ARCHITECTURAL_WIRING_KINDS
            or (cs.action or "").strip().lower() != "ensure_tests"
            or cs.metrics_delta.get("followup")
        ):
            out.append(cs)
            continue
        target_units = [
            units_by_id[t.unit_id]
            for t in cs.targets
            if t.unit_id and t.unit_id in units_by_id
        ]
        wiring_units = _resolve_wiring_integration_units(cs, target_units, units)
        if not wiring_units:
            out.append(cs)
            continue
        method_names = [u.qualified_name.rsplit(".", 1)[-1] for u in wiring_units[:3]]
        title = f"Ensure Kafka integration tests for {', '.join(method_names)}"
        rationale = (
            f"{len(wiring_units)} handler method(s) need @EmbeddedKafka or Testcontainers "
            "characterization before wire-vs-remove decisions. "
            "Document-only prep does not substitute for integration coverage."
        )
        out.append(
            cs.model_copy(
                update={
                    "title": title,
                    "rationale": rationale,
                    "targets": _targets_for_units(wiring_units),
                    "brief": _ensure_tests_brief(
                        wiring_units,
                        title=title,
                        threshold=80.0,
                        integration_wiring=True,
                    ),
                }
            )
        )
    return out


def _make_followup(
    *,
    parent: Changeset,
    action: str,
    title: str,
    rationale: str,
    units: list[CodeUnit],
    confidence: float = FOLLOWUP_CONFIDENCE,
    extra_metrics: dict | None = None,
) -> Changeset:
    targets = _targets_for_units(units)
    locs = []
    for u in units:
        locs.append(
            max(
                1,
                (u.span.end_line - u.span.start_line + 1)
                if u.span.end_line
                else (u.source_snippet or "").count("\n") + 1,
            )
        )
    metrics = {
        "estimated_loc_removed": 0,
        "loc_under_review": sum(locs),
        "target_locs": locs,
        "goal": action,
        "cross_repo": len({t.repo for t in targets}) >= 2,
        "parent_changeset_id": parent.id,
        "followup": True,
        **(extra_metrics or {}),
    }
    brief = (
        f"## {title}\n\n"
        f"Action: `{action}`\nConfidence: {confidence:.2f}\n"
        f"Follow-up of `{parent.id}` ({parent.action}).\n\n"
        f"{rationale}\n\n### Targets\n"
        + "\n".join(f"- `{t.repo}/{t.path}` :: `{t.symbol}`" for t in targets)
        + "\n\nAsk the user before applying (HITL)."
    )
    return Changeset(
        id=content_hash(f"cs:follow:{action}:{parent.id}:{','.join(u.id for u in units)}")[:16],
        title=title,
        action=action,
        brief=brief,
        rationale=rationale,
        confidence=confidence,
        targets=targets,
        parallel_group=f"g-{action}-followup",
        evidence_refs=list(parent.evidence_refs),
        metrics_delta=metrics,
    )


def _followup_dedupe_key(cs: Changeset) -> tuple[str, str, tuple[str, ...]]:
    action = (cs.action or "").strip().lower()
    unit_ids = tuple(sorted(t.unit_id for t in cs.targets if t.unit_id))
    if action == "ensure_tests" and unit_ids:
        # Collapse primary vs follow-up ensure_tests that share unit_ids.
        return (action, "", unit_ids)
    parent = str(cs.metrics_delta.get("parent_changeset_id") or "")
    return (action, parent, unit_ids)


def _is_repo_root_only(cs: Changeset) -> bool:
    if not cs.targets:
        return True
    return all(
        (t.path or "").replace("\\", "/").strip() in {"", ".", t.repo}
        for t in cs.targets
    )


def _followup_fingerprint(cs: Changeset) -> tuple[str, str]:
    return ((cs.action or "").strip().lower(), (cs.rationale or "").strip())


def _target_scope_score(t: ChangesetTarget) -> int:
    path = (t.path or "").replace("\\", "/").strip()
    score = 0
    if path and path not in {".", "/"} and not path.endswith("/"):
        score += 2
    if t.symbol:
        score += 1
    if t.unit_id:
        score += 1
    if path in {"", ".", t.repo}:
        score -= 2
    return score


def _changeset_scope_score(cs: Changeset) -> int:
    if not cs.targets:
        return -10
    return sum(_target_scope_score(t) for t in cs.targets)


def _followup_unit_ids(cs: Changeset) -> set[str]:
    return {t.unit_id for t in cs.targets if t.unit_id}


def _followups_overlap(a: Changeset, b: Changeset) -> bool:
    if _followup_unit_ids(a) & _followup_unit_ids(b):
        return True
    repos_a = {t.repo for t in a.targets}
    repos_b = {t.repo for t in b.targets}
    for repo in repos_a & repos_b:
        a_root = any(_target_scope_score(t) <= 0 and t.repo == repo for t in a.targets)
        b_root = any(_target_scope_score(t) <= 0 and t.repo == repo for t in b.targets)
        a_file = any(_target_scope_score(t) > 0 and t.repo == repo for t in a.targets)
        b_file = any(_target_scope_score(t) > 0 and t.repo == repo for t in b.targets)
        if (a_root and b_file) or (b_root and a_file):
            return True
    return False


def _strip_repo_root_doc_duplicates(changesets: list[Changeset]) -> list[Changeset]:
    """Drop repo-root document items when a file-scoped sibling exists (primary or follow-up)."""
    docs = [c for c in changesets if c.action == "document"]
    if not docs:
        return changesets

    file_docs = [c for c in docs if not _is_repo_root_only(c)]
    if not file_docs:
        return changesets

    file_repos = {t.repo for c in file_docs for t in c.targets if t.repo}

    file_parents = {str(c.metrics_delta.get("parent_changeset_id") or "") for c in file_docs}
    file_evidence = {frozenset(c.evidence_refs or []) for c in file_docs}
    file_symbols = {t.symbol for c in file_docs for t in c.targets if t.symbol}
    file_paths = {
        (t.path or "").replace("\\", "/").rsplit("/", 1)[-1]
        for c in file_docs
        for t in c.targets
        if (t.path or "").replace("\\", "/").strip() not in {"", ".", c.targets[0].repo if c.targets else ""}
    }
    file_kinds = {
        str((c.metrics_delta or {}).get("kind") or "")
        for c in file_docs
        if (c.metrics_delta or {}).get("kind")
    }

    out: list[Changeset] = []
    for cs in changesets:
        if cs.action == "document" and _is_repo_root_only(cs):
            repos = {t.repo for t in cs.targets if t.repo}
            if repos & file_repos:
                continue
            parent = str(cs.metrics_delta.get("parent_changeset_id") or "")
            ev = frozenset(cs.evidence_refs or [])
            symbols = {t.symbol for t in cs.targets if t.symbol}
            kind = str((cs.metrics_delta or {}).get("kind") or "")
            if parent and parent in file_parents:
                continue
            if ev and ev in file_evidence:
                continue
            if symbols & file_symbols:
                continue
            if kind and kind in file_kinds:
                continue
            if any(sym and sym in (cs.title or "") for sym in file_symbols):
                continue
            # Same draft_map / slop rationale as a file-scoped sibling
            if any(
                (cs.rationale or "").strip() == (fd.rationale or "").strip()
                and (cs.rationale or "").strip()
                for fd in file_docs
            ):
                continue
            if file_paths and "draft" in (cs.title or "").lower():
                continue
        out.append(cs)
    return out


def _collapse_duplicate_followups(changesets: list[Changeset]) -> list[Changeset]:
    """Drop duplicate prep items; primary ensure_tests wins over follow-up siblings."""
    ensure_all = [
        c for c in changesets if (c.action or "").strip().lower() == "ensure_tests"
    ]
    followups = [c for c in changesets if c.metrics_delta.get("followup")]
    if len(ensure_all) >= 2:
        rest = [
            c
            for c in changesets
            if (c.action or "").strip().lower() != "ensure_tests"
        ]
        by_key: dict[tuple[str, str, tuple[str, ...]], list[Changeset]] = {}
        for cs in ensure_all:
            by_key.setdefault(_followup_dedupe_key(cs), []).append(cs)
        kept: list[Changeset] = []
        dropped_ids: set[str] = set()
        for group in by_key.values():
            if len(group) == 1:
                kept.append(group[0])
                continue
            best = max(
                group,
                key=lambda c: (
                    0 if c.metrics_delta.get("followup") else 1,
                    _changeset_scope_score(c),
                    c.confidence,
                ),
            )
            kept.append(best)
            for cs in group:
                if cs.id != best.id:
                    dropped_ids.add(cs.id)
        if dropped_ids:
            def _scrub_deps(cs: Changeset) -> Changeset:
                deps = [d for d in (cs.depends_on or []) if d not in dropped_ids]
                if deps == (cs.depends_on or []):
                    return cs
                return cs.model_copy(update={"depends_on": deps})

            rest = [_scrub_deps(c) for c in rest]
        changesets = rest + kept
        followups = [c for c in changesets if c.metrics_delta.get("followup")]

    if len(followups) < 2:
        return _strip_repo_root_doc_duplicates(changesets)

    rest = [c for c in changesets if not c.metrics_delta.get("followup")]
    by_key: dict[tuple[str, str, tuple[str, ...]], list[Changeset]] = {}
    for cs in followups:
        by_key.setdefault(_followup_dedupe_key(cs), []).append(cs)

    kept: list[Changeset] = []
    dropped_ids: set[str] = set()
    for group in by_key.values():
        if len(group) == 1:
            kept.append(group[0])
            continue
        best = max(
            group,
            key=lambda c: (
                0 if c.metrics_delta.get("followup") else 1,
                _changeset_scope_score(c),
                c.confidence,
            ),
        )
        kept.append(best)
        for cs in group:
            if cs.id != best.id:
                dropped_ids.add(cs.id)

    if dropped_ids:
        def _scrub_deps(cs: Changeset) -> Changeset:
            deps = [d for d in (cs.depends_on or []) if d not in dropped_ids]
            if deps == (cs.depends_on or []):
                return cs
            return cs.model_copy(update={"depends_on": deps})

        rest = [_scrub_deps(c) for c in rest]

    return _strip_repo_root_doc_duplicates(rest + kept)


def _folded_parent_ids(changesets: list[Changeset]) -> set[str]:
    folded: set[str] = set()
    for cs in changesets:
        for fid in (cs.metrics_delta or {}).get("folded_changeset_ids") or []:
            folded.add(str(fid))
    return folded


def _batch_document_followup(group: list[Changeset]) -> Changeset:
    """Merge overlapping document follow-ups into one opt-in prep item."""
    if len(group) == 1:
        return group[0]
    seen_units: dict[str, ChangesetTarget] = {}
    parent_ids: list[str] = []
    evidence: list[str] = []
    for cs in group:
        parent = str((cs.metrics_delta or {}).get("parent_changeset_id") or "")
        if parent:
            parent_ids.append(parent)
        for ref in cs.evidence_refs or []:
            if ref not in evidence:
                evidence.append(ref)
        for t in cs.targets or []:
            if t.unit_id and t.unit_id not in seen_units:
                seen_units[t.unit_id] = t
            elif not t.unit_id:
                key = (t.repo, t.path, t.symbol)
                if key not in {(x.repo, x.path, x.symbol) for x in seen_units.values()}:
                    seen_units[f"anon:{len(seen_units)}"] = t
    targets = list(seen_units.values())
    n_parents = len(set(parent_ids)) or len(group)
    title = f"Document gaps (batched prep for {n_parents} structural items)"
    rationale = (
        f"Batched {len(group)} overlapping document prep follow-up(s) into one opt-in item "
        f"covering {len(targets)} unit(s). Prefer before structural refactors when the user opts in."
    )
    metrics = {
        "estimated_loc_removed": 0,
        "loc_under_review": sum(
            int((cs.metrics_delta or {}).get("loc_under_review") or 0) for cs in group
        ),
        "goal": "document",
        "followup": True,
        "batched_document_prep": True,
        "batched_from": [cs.id for cs in group],
        "batched_parent_ids": sorted(set(parent_ids)),
        "needs_write_docs": True,
        "doc_gap_count": len(targets),
    }
    brief = (
        f"## {title}\n\n"
        f"Action: `document`\nConfidence: {FOLLOWUP_CONFIDENCE:.2f}\n"
        f"Batched follow-up of {n_parents} structural parent(s).\n\n"
        f"{rationale}\n\n### Targets\n"
        + "\n".join(f"- `{t.repo}/{t.path}` :: `{t.symbol}`" for t in targets)
        + "\n\nAsk the user before applying (HITL)."
    )
    unit_key = ",".join(sorted(t.unit_id or t.symbol or "" for t in targets))
    return Changeset(
        id=content_hash(f"cs:follow:document:batch:{unit_key}")[:16],
        title=title,
        action="document",
        brief=brief,
        rationale=rationale,
        confidence=FOLLOWUP_CONFIDENCE,
        targets=targets,
        parallel_group="g-document-followup",
        evidence_refs=evidence,
        metrics_delta=metrics,
    )


def _batch_ensure_tests_prep(changesets: list[Changeset]) -> list[Changeset]:
    """Merge all ensure_tests into one prep item — user decides yes/no once at prep phase."""
    ensure_all = [
        c for c in changesets if (c.action or "").strip().lower() == "ensure_tests"
    ]
    if len(ensure_all) <= 1:
        return changesets
    old_ids = {c.id for c in ensure_all}
    seen_units: dict[str, ChangesetTarget] = {}
    evidence: list[str] = []
    for cs in ensure_all:
        for ref in cs.evidence_refs or []:
            if ref not in evidence:
                evidence.append(ref)
        for t in cs.targets or []:
            key = t.unit_id or f"{t.repo}:{t.path}:{t.symbol}"
            if key not in seen_units:
                seen_units[key] = t
    targets = list(seen_units.values())
    n_sources = len(ensure_all)
    title = f"Raise test coverage before structural refactors ({len(targets)} unit(s))"
    rationale = (
        f"Batched {n_sources} ensure_tests prep item(s) into one coverage-first gate. "
        "User opts in or out once at prep phase — not per class."
    )
    conf = max(float(c.confidence) for c in ensure_all)
    metrics = {
        "estimated_loc_removed": 0,
        "loc_under_review": sum(
            int((cs.metrics_delta or {}).get("loc_under_review") or 0) for cs in ensure_all
        ),
        "goal": "ensure_tests",
        "batched_ensure_tests_prep": True,
        "batched_from": sorted(old_ids),
        "batched_count": n_sources,
    }
    brief = (
        f"## {title}\n\n"
        f"Action: `ensure_tests`\nConfidence: {conf:.2f}\n"
        f"**Prep-only decision** — ask once before queue walk whether to push coverage to the "
        f"limit on all flagged production code. Do not ask per target.\n\n"
        f"{rationale}\n\n### Targets\n"
        + "\n".join(
            f"- `{t.repo}/{t.path}` :: `{t.symbol}`" for t in targets[:40]
        )
        + (f"\n- … and {len(targets) - 40} more" if len(targets) > 40 else "")
    )
    unit_key = ",".join(sorted(seen_units.keys()))
    batched = Changeset(
        id=content_hash(f"cs:prep:ensure_tests:batch:{unit_key}")[:16],
        title=title,
        action="ensure_tests",
        brief=brief,
        rationale=rationale,
        confidence=conf,
        targets=targets,
        parallel_group="g-ensure-tests-prep",
        evidence_refs=evidence,
        metrics_delta=metrics,
    )

    def _remap_deps(cs: Changeset) -> Changeset:
        deps = list(cs.depends_on or [])
        if not deps:
            return cs
        remapped: list[str] = []
        saw_batched = False
        for dep in deps:
            if dep in old_ids:
                if batched.id not in remapped:
                    remapped.append(batched.id)
                saw_batched = True
            else:
                remapped.append(dep)
        if not saw_batched and remapped == deps:
            return cs
        return cs.model_copy(update={"depends_on": remapped})

    rest = [c for c in changesets if c.id not in old_ids]
    rest = [_remap_deps(c) for c in rest]
    return rest + [batched]


def _consolidate_document_followups(changesets: list[Changeset]) -> list[Changeset]:
    """Skip orphaned doc follow-ups; batch overlapping siblings into one opt-in prep item."""
    primary_by_id = {
        cs.id: cs
        for cs in changesets
        if not (cs.metrics_delta or {}).get("followup")
    }
    primary_ids = set(primary_by_id)
    folded = _folded_parent_ids(changesets)
    primaries: list[Changeset] = []
    doc_followups: list[Changeset] = []
    other: list[Changeset] = []

    for cs in changesets:
        if not (cs.metrics_delta or {}).get("followup"):
            primaries.append(cs)
            continue
        action = (cs.action or "").strip().lower()
        if action != "document":
            other.append(cs)
            continue
        parent = str((cs.metrics_delta or {}).get("parent_changeset_id") or "")
        if parent and (parent not in primary_ids or parent in folded):
            continue
        parent_cs = primary_by_id.get(parent)
        if parent_cs is not None:
            parent_action = (parent_cs.action or "").strip().lower()
            if parent_action in {"merge", "delete_duplicate"}:
                continue
            if parent_cs.status == ChangesetStatus.SKIPPED:
                continue
        doc_followups.append(cs)

    if len(doc_followups) <= 1:
        return primaries + other + doc_followups

    if len(doc_followups) >= 3:
        return primaries + other + [_batch_document_followup(doc_followups)]

    remaining = list(doc_followups)
    batched: list[Changeset] = []
    while remaining:
        seed = remaining.pop(0)
        cluster = [seed]
        i = 0
        while i < len(remaining):
            if _followups_overlap(seed, remaining[i]) or _followups_overlap(
                cluster[0], remaining[i]
            ):
                cluster.append(remaining.pop(i))
            else:
                i += 1
        batched.append(_batch_document_followup(cluster))

    if len(batched) >= 4:
        batched = [_batch_document_followup(batched)]

    return primaries + other + batched


def _file_scoped_doc_units(doc_units: list[CodeUnit]) -> list[CodeUnit]:
    """Prefer file-scoped units; skip repo-root placeholders when file targets exist."""
    file_scoped = [
        u
        for u in doc_units
        if (u.path or "").replace("\\", "/").strip() not in {"", ".", u.repo}
    ]
    return file_scoped if file_scoped else doc_units


def attach_quality_followups(
    changesets: list[Changeset],
    units: list[CodeUnit],
    *,
    coverage: CoverageSnapshot | None = None,
    doc_gaps: list[DocumentGap] | None = None,
    coverage_threshold: float = 80.0,
    followup_confidence: float = FOLLOWUP_CONFIDENCE,
    workspace: Path | None = None,
    repos: list[Path] | None = None,
    existing_tests: list[str] | None = None,
    coverage_finding_count: int = 0,
    describe_mode: str = "describe",
) -> list[Changeset]:
    """Emit document/ensure_tests siblings at low confidence; wire split→tests depends_on."""
    if not changesets:
        return changesets

    emit_document = (describe_mode or "describe").strip().lower() == "describe"

    if existing_tests is None and repos:
        existing_tests = inventory_test_files(repos)
    elif existing_tests is None and workspace is not None:
        ws = Path(workspace).expanduser().resolve()
        if ws.is_dir():
            child_repos = [
                p
                for p in sorted(ws.iterdir())
                if p.is_dir()
                and not p.name.startswith(".")
                and any((p / m).exists() for m in ("pom.xml", "pyproject.toml", "go.mod", "package.json"))
            ]
            existing_tests = inventory_test_files(child_repos or [ws])
        else:
            existing_tests = []
    else:
        existing_tests = existing_tests or []

    coverage_pressure = _coverage_goal_pressure(
        changesets,
        coverage=coverage,
        coverage_finding_count=coverage_finding_count,
    )
    index_empty = _coverage_index_empty(coverage)

    changesets = _remap_primary_ensure_tests(
        changesets,
        units,
        coverage=coverage,
        existing_tests=existing_tests,
        coverage_threshold=coverage_threshold,
        coverage_pressure=coverage_pressure,
    )
    changesets = _remap_wiring_primary_targets(changesets, units)

    primary_ensure_unit_ids: set[str] = set()
    for cs in changesets:
        if (cs.action or "").strip().lower() != "ensure_tests":
            continue
        if (cs.metrics_delta or {}).get("followup"):
            continue
        primary_ensure_unit_ids.update(t.unit_id for t in cs.targets if t.unit_id)

    units_by_id = {u.id: u for u in units}
    production_by_repo = index_production_units_by_repo(units)
    if doc_gaps is None:
        # Only leaf-ish units that appear as targets
        target_units = []
        seen: set[str] = set()
        for cs in changesets:
            for t in cs.targets:
                uid = t.unit_id
                if uid and uid in units_by_id and uid not in seen:
                    seen.add(uid)
                    target_units.append(units_by_id[uid])
        doc_gaps = (
            find_document_gaps(
                target_units,
                languages={u.language.lower() for u in target_units if u.language},
            )
            if target_units
            else []
        )
    gaps_by_unit = {g.unit_id: g for g in doc_gaps}

    out: list[Changeset] = []
    followups: list[Changeset] = []

    for cs in changesets:
        if cs.metrics_delta.get("followup"):
            out.append(cs)
            continue

        target_units: list[CodeUnit] = []
        for t in cs.targets:
            if t.unit_id and t.unit_id in units_by_id:
                target_units.append(units_by_id[t.unit_id])

        doc_units = _file_scoped_doc_units([u for u in target_units if u.id in gaps_by_unit])
        action_l = (cs.action or "").strip().lower()
        # Empty coverage index: structural parents only keep a flag; skip O(units) resolve.
        skip_cov_resolve = (
            index_empty
            and not _is_draft_map_parent(cs)
            and not coverage_pressure
            and action_l not in {"split", "extract_shared", "move"}
        )
        cov_units = (
            []
            if skip_cov_resolve
            else _resolve_ensure_test_units(
                target_units,
                units,
                coverage=coverage,
                parent=cs,
                existing_tests=existing_tests,
                coverage_threshold=coverage_threshold,
                coverage_pressure=coverage_pressure,
                production_by_repo=production_by_repo,
            )
        )
        if index_empty and not _is_draft_map_parent(cs) and not coverage_pressure:
            if action_l not in {"split", "extract_shared", "move"}:
                cov_units = []
        if primary_ensure_unit_ids:
            cov_units = [u for u in cov_units if u.id not in primary_ensure_unit_ids]

        wiring_kind = str((cs.metrics_delta or {}).get("kind") or "")
        wiring_units = (
            _resolve_wiring_integration_units(cs, target_units, units)
            if wiring_kind in _ARCHITECTURAL_WIRING_KINDS
            else []
        )
        ensure_units = wiring_units or cov_units

        metrics = dict(cs.metrics_delta or {})
        coverage_low = (
            coverage is not None
            and coverage.overall_percent is not None
            and float(coverage.overall_percent) < 50
        )
        if doc_units:
            metrics["needs_write_docs"] = True
            metrics["doc_gap_count"] = len(doc_units)
        if ensure_units:
            metrics["needs_coverage"] = True
            metrics["coverage_gap_count"] = len(ensure_units)
        elif (cs.action or "").strip().lower() in {"split", "extract_shared", "move"}:
            if index_empty:
                metrics["coverage_index_empty"] = True
            if index_empty or coverage_low:
                metrics["needs_coverage"] = True

        depends = list(cs.depends_on or [])
        ensure_cs: Changeset | None = None

        parent_kind = str((cs.metrics_delta or {}).get("kind") or "")
        # Don't duplicate if this CS is already document/ensure_tests
        skip_doc_followup = (
            coverage_first_mode(out + followups)
            or bool(primary_ensure_unit_ids)
            or bool(ensure_units and cs.action not in {"ensure_tests"})
            or parent_kind in _SLOP_PARENT_KINDS
        )
        if (
            emit_document
            and doc_units
            and cs.action not in {"document"}
            and not skip_doc_followup
        ):
            followups.append(
                _make_followup(
                    parent=cs,
                    action="document",
                    title=f"Document gaps for: {cs.title}",
                    rationale=(
                        f"{len(doc_units)} target unit(s) missing replaceable in-source docs. "
                        "Prep item — prefer before structural refactors when the user opts in."
                    ),
                    units=doc_units,
                    confidence=followup_confidence,
                    extra_metrics={"needs_write_docs": True, "doc_gap_count": len(doc_units)},
                )
            )

        if ensure_units and cs.action not in {"ensure_tests"}:
            if wiring_units:
                method_names = [
                    u.qualified_name.rsplit(".", 1)[-1] for u in wiring_units[:3]
                ]
                ensure_title = (
                    f"Ensure Kafka integration tests for {', '.join(method_names)}"
                )
                ensure_rationale = (
                    f"{len(wiring_units)} handler method(s) need @EmbeddedKafka or "
                    "Testcontainers characterization before wire-vs-remove decisions. "
                    "Document-only prep does not substitute for integration coverage."
                )
            else:
                ensure_title = f"Ensure tests before/with: {cs.title}"
                ensure_rationale = (
                    f"{len(ensure_units)} production target(s) need characterization coverage "
                    f"(below {coverage_threshold:.0f}% or draft-map caller branch). "
                    "Write or expand real tests until Jacoco-relevant behavior is covered — "
                    "a test file existing is not enough. "
                    "Do not expand Jacoco padding / symbol-exists-only tests."
                )
            ensure_cs = _make_followup(
                parent=cs,
                action="ensure_tests",
                title=ensure_title,
                rationale=ensure_rationale,
                units=ensure_units,
                confidence=followup_confidence,
                extra_metrics={
                    "needs_coverage": True,
                    "coverage_gap_count": len(ensure_units),
                },
            )
            ensure_cs = ensure_cs.model_copy(
                update={
                    "brief": _ensure_tests_brief(
                        ensure_units,
                        title=ensure_title,
                        threshold=coverage_threshold,
                        coverage_index_empty=index_empty,
                        coverage_estimated=_coverage_is_estimated(coverage),
                        integration_wiring=bool(wiring_units),
                    )
                }
            )
            followups.append(ensure_cs)
            if cs.action == "split":
                depends.append(ensure_cs.id)

        out.append(
            cs.model_copy(
                update={
                    "metrics_delta": metrics,
                    "depends_on": depends,
                }
            )
        )

    combined = out + followups
    combined = _collapse_duplicate_followups(combined)
    combined = _batch_ensure_tests_prep(combined)
    combined = _consolidate_document_followups(combined)
    combined = _wire_structural_wiring_deps(combined)
    cov_first = coverage_first_mode(combined)
    combined.sort(
        key=lambda c: (
            prep_rank(c, coverage_first=cov_first),
            -int(bool(c.metrics_delta.get("cross_repo"))),
            -float(c.confidence),
            -int(c.metrics_delta.get("estimated_loc_removed") or 0),
        )
    )
    return combined
