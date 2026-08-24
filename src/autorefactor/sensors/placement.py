from __future__ import annotations

import re
from pathlib import Path

import numpy as np

from autorefactor.domain import (
    AnalysisGoal,
    Finding,
    Severity,
    SuggestedAction,
    UnitKind,
    content_hash,
    try_make_finding,
)
from autorefactor.index.hierarchy import simple_name
from autorefactor.sensors.base import AnalysisContext
from autorefactor.sensors.similarity import _is_low_signal, cosine, is_test_path

# Roles that must not be mixed by embedding-only placement moves.
_DTO_SUFFIXES = (
    "data",
    "dto",
    "request",
    "response",
    "entity",
    "model",
    "vo",
    "pojo",
    "record",
)
_ROLE_PACKAGE = (
    ("mapper", ("/mapper/", "/mappers/")),
    ("controller", ("/controller/", "/controllers/", "/resource/", "/resources/", "/endpoint/")),
    ("repository", ("/repository/", "/repositories/", "/dao/", "/persistence/")),
    ("service", ("/service/", "/services/")),
    ("dto", ("/dto/", "/dtos/", "/model/", "/models/", "/domain/value/")),
)


def architectural_role(path: str, qualified_name: str) -> str | None:
    """Coarse layer role from path / type name (Java-ish layouts)."""
    p = (path or "").replace("\\", "/").lower()
    qn = (qualified_name or "").strip()
    simple = qn.rsplit(".", 1)[-1] if qn else Path(p).stem
    simple_l = simple.lower()
    # Nested *Data / *Response / *Entity wins over the outer service file path.
    if simple_l.endswith(_DTO_SUFFIXES) and not simple_l.endswith(
        ("service", "mapper", "controller", "repository", "handler")
    ):
        return "dto"
    if simple_l.endswith("mapper") or "/mapper/" in p or "/mappers/" in p:
        return "mapper"
    stem = Path(p).stem.lower()
    if "mapper" in stem:
        return "mapper"
    for role, markers in _ROLE_PACKAGE:
        if any(m in p for m in markers):
            return role
    if simple_l.endswith("service") or simple_l.endswith("services"):
        return "service"
    if simple_l.endswith(("controller", "resource", "endpoint")):
        return "controller"
    if simple_l.endswith(("repository", "dao")):
        return "repository"
    return None


def role_for_unit(unit) -> str | None:
    """Role for a function (owner type) or type unit."""
    path = getattr(unit, "path", "") or ""
    qn = getattr(unit, "qualified_name", "") or ""
    kind = getattr(unit, "kind", None)
    if kind == UnitKind.FUNCTION and "." in qn:
        return architectural_role(path, qn.rsplit(".", 1)[0])
    return architectural_role(path, qn)


def cross_layer_placement(fn, target) -> bool:
    """True when moving ``fn`` onto ``target`` crosses incompatible layers."""
    src = role_for_unit(fn)
    dst = role_for_unit(target)
    if src is None or dst is None:
        return False
    if src == dst:
        return False
    # Mapper methods belong in mappers; DTOs are not homes for mapper logic.
    if "mapper" in {src, dst}:
        return True
    if dst == "dto" and src in {"service", "controller", "repository"}:
        return True
    return False


def _target_named_in_signature(fn, target) -> bool:
    """True when the target type name appears in the method signature (param affinity)."""
    snippet = (getattr(fn, "source_snippet", None) or "").strip()
    if not snippet:
        return False
    # Signature ≈ text before first '{' or first line for abstract methods.
    head = snippet.split("{", 1)[0]
    if "\n" in head:
        head = "\n".join(head.splitlines()[:8])
    target_name = simple_name(getattr(target, "qualified_name", "") or "")
    if not target_name or len(target_name) < 3:
        return False
    return bool(re.search(rf"\b{re.escape(target_name)}\b", head))


def incompatible_placement_move(fn, target) -> bool:
    """Reject embedding placement FPs: cross-layer or param-type affinity into DTO/mapper."""
    if cross_layer_placement(fn, target):
        return True
    dst = role_for_unit(target)
    if dst in {"dto", "mapper"} and _target_named_in_signature(fn, target):
        # Method that takes AggregatedBonusData embedding-matches the DTO —
        # that is expected coupling, not a reason to relocate the method onto it.
        return True
    return False


def _is_interface_or_abstract_type(unit) -> bool:
    snippet = (unit.source_snippet or "").lstrip()
    return snippet.startswith("interface ") or snippet.startswith("abstract class ") or bool(
        re.search(r"\binterface\s+\w+", snippet[:120])
    )


def _methods_from_type_snippet(snippet: str) -> set[str]:
    """Parse method names from a TYPE source snippet (survives trivial-unit filtering)."""
    names: set[str] = set()
    skip = {
        "if",
        "for",
        "while",
        "switch",
        "catch",
        "return",
        "new",
        "class",
        "interface",
        "enum",
        "record",
        "throws",
        "extends",
        "implements",
        "import",
        "package",
        "public",
        "private",
        "protected",
        "static",
        "final",
        "abstract",
        "default",
        "synchronized",
        "native",
        "void",
        "boolean",
        "byte",
        "char",
        "short",
        "int",
        "long",
        "float",
        "double",
    }
    for m in re.finditer(
        r"(?:^|\n)\s*(?:(?:public|protected|private|default|static|abstract|"
        r"final|synchronized|native|strictfp)\s+)*"
        r"(?:[\w.<>,\[\]?]+\s+)+(\w+)\s*\(",
        snippet,
    ):
        name = m.group(1)
        if name not in skip:
            names.add(name)
    return names


def _type_declared_methods(ctx: AnalysisContext) -> dict[str, set[str]]:
    """Map TYPE unit id → declared method simple names (indexed FUNCTION children)."""
    methods: dict[str, set[str]] = {}
    for unit in ctx.units:
        if unit.kind != UnitKind.FUNCTION:
            continue
        owner_id = unit.parent_id
        if not owner_id and "." in unit.qualified_name:
            parent_qn = unit.qualified_name.rsplit(".", 1)[0]
            for t in ctx.units:
                if t.kind == UnitKind.TYPE and t.repo == unit.repo and t.qualified_name == parent_qn:
                    owner_id = t.id
                    break
        if not owner_id:
            continue
        methods.setdefault(owner_id, set()).add(simple_name(unit.qualified_name))
    return methods


def _interface_methods_by_qn(ctx: AnalysisContext, type_methods: dict[str, set[str]]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for unit in ctx.units:
        if unit.kind != UnitKind.TYPE or not _is_interface_or_abstract_type(unit):
            continue
        names = set(type_methods.get(unit.id, set()))
        names |= _methods_from_type_snippet(unit.source_snippet or "")
        out[unit.qualified_name] = names
        type_methods.setdefault(unit.id, set()).update(names)
    return out


def _method_already_on_target(
    fn,
    best,
    *,
    type_methods: dict[str, set[str]],
    interface_by_qn: dict[str, set[str]],
) -> bool:
    method = simple_name(fn.qualified_name)
    if method in type_methods.get(best.id, set()):
        return True
    if method in interface_by_qn.get(best.qualified_name, set()):
        return True
    return False


class PlacementSensor:
    id = "placement"
    goal = AnalysisGoal.PLACEMENT

    def available(self, ctx: AnalysisContext) -> bool:
        fns = sum(1 for u in ctx.units if u.kind == UnitKind.FUNCTION and u.embedding)
        types = sum(1 for u in ctx.units if u.kind == UnitKind.TYPE and u.embedding)
        return fns >= 1 and types >= 2

    def collect(self, ctx: AnalysisContext) -> list[Finding]:
        floor = max(0.82, ctx.config.similarity_threshold)
        if ctx.focus == "strict":
            floor = max(floor, 0.88)
        margin = 0.05
        functions = [
            u
            for u in ctx.units
            if u.kind == UnitKind.FUNCTION
            and u.embedding
            and not is_test_path(u.path)
            and not _is_low_signal(u)
        ]
        types = [u for u in ctx.units if u.kind == UnitKind.TYPE and u.embedding]
        known = ctx.known_ids()
        if not types:
            return []
        type_matrix = np.array([t.embedding for t in types], dtype=np.float64)
        type_methods = _type_declared_methods(ctx)
        interface_by_qn = _interface_methods_by_qn(ctx, type_methods)
        findings: list[Finding] = []
        max_flags = ctx.config.max_similarity_pairs

        for fn in functions:
            owner_id = None
            parent_qn = fn.qualified_name.rsplit(".", 1)[0] if "." in fn.qualified_name else None
            for t in types:
                if t.repo == fn.repo and t.qualified_name == parent_qn:
                    owner_id = t.id
                    break
            if owner_id is None and fn.parent_id:
                owner_id = fn.parent_id

            fn_vec = np.array(fn.embedding, dtype=np.float64)
            best_i, best_sim = -1, 0.0
            owner_sim = 0.0
            for i, t in enumerate(types):
                if t.repo != fn.repo:
                    continue
                sim = cosine(fn_vec, type_matrix[i])
                if owner_id and t.id == owner_id:
                    owner_sim = sim
                if sim > best_sim:
                    best_sim = sim
                    best_i = i
            if best_i < 0:
                continue
            best = types[best_i]

            if best_sim < floor:
                continue

            if owner_id and best.id == owner_id:
                continue
            if owner_id and best_sim < owner_sim + margin:
                continue

            if _method_already_on_target(
                fn,
                best,
                type_methods=type_methods,
                interface_by_qn=interface_by_qn,
            ):
                continue

            if incompatible_placement_move(fn, best):
                continue

            f = try_make_finding(
                id=content_hash(f"placement:move:{fn.id}:{best.id}"),
                goal=self.goal,
                sensor=self.id,
                severity=Severity.HIGH if best_sim >= 0.92 else Severity.MEDIUM,
                title=(
                    f"Place {simple_name(fn.qualified_name)} into "
                    f"{best.qualified_name} (cosine={best_sim:.3f})"
                ),
                evidence=f"method↔type cosine={best_sim:.3f}; owner_sim={owner_sim:.3f}",
                unit_ids=[fn.id, best.id],
                action_hint=SuggestedAction.MOVE,
                score=best_sim,
                metrics={
                    "best_cosine": best_sim,
                    "owner_cosine": owner_sim,
                    "new_type": 0.0,
                },
                known_ids=known,
            )
            if f:
                findings.append(f)
            if len(findings) >= max_flags:
                break
        return findings
