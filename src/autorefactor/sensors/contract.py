"""Cross-language contract drift — proto coverage, naming maps, surface parity."""

from __future__ import annotations

import re
from collections import defaultdict

from autorefactor.domain import (
    AnalysisGoal,
    CodeUnit,
    Finding,
    Severity,
    SuggestedAction,
    UnitKind,
    content_hash,
    try_make_finding,
)
from autorefactor.index.hierarchy import simple_name
from autorefactor.sensors.base import AnalysisContext
from autorefactor.sensors.similarity import is_test_path
from autorefactor.testing.inventory import looks_like_test_path

_CONTRACT_TEST_HINTS = re.compile(
    r"contract|golden|conformance|proto|grpc|vector",
    re.IGNORECASE,
)


def _to_snake(name: str) -> str:
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s)
    return s.lower().replace("-", "_")


def _naming_variants(name: str) -> dict[str, str]:
    snake = _to_snake(name)
    parts = snake.split("_")
    camel = parts[0] + "".join(p.capitalize() for p in parts[1:]) if parts else snake
    pascal = "".join(p.capitalize() for p in parts) if parts else snake
    return {"snake_case": snake, "camelCase": camel, "PascalCase": pascal}


def _impl_units(units: list[CodeUnit]) -> list[CodeUnit]:
    return [
        u
        for u in units
        if u.kind in {UnitKind.FUNCTION, UnitKind.TYPE}
        and not is_test_path(u.path)
        and u.language in {"python", "go", "typescript", "javascript", "java"}
    ]


def _proto_rpcs(units: list[CodeUnit]) -> dict[str, CodeUnit]:
    out: dict[str, CodeUnit] = {}
    for u in units:
        if u.kind == UnitKind.PROTO_RPC:
            out[_to_snake(simple_name(u.qualified_name))] = u
    return out


def _has_contract_tests(existing_paths: list[str], repo: str, canonical: str) -> bool:
    for p in existing_paths:
        if not p.startswith(f"{repo}/"):
            continue
        if not looks_like_test_path(p):
            continue
        stem = p.lower()
        if canonical in stem.replace("-", "_") and _CONTRACT_TEST_HINTS.search(p):
            return True
    return False


class ContractSensor:
    id = "contract"
    goal = AnalysisGoal.CLUSTER

    def available(self, ctx: AnalysisContext) -> bool:
        return len(_impl_units(list(ctx.units))) >= 2

    def collect(self, ctx: AnalysisContext) -> list[Finding]:
        known = ctx.known_ids()
        units = list(ctx.units)
        impls = _impl_units(units)
        rpcs = _proto_rpcs(units)

        by_canonical: dict[str, list[CodeUnit]] = defaultdict(list)
        for u in impls:
            by_canonical[_to_snake(simple_name(u.qualified_name))].append(u)

        test_paths = [
            u.path
            for u in units
            if looks_like_test_path(u.path)
        ]
        # Also scan indexed FILE units under tests/
        for u in units:
            if u.kind == UnitKind.FILE and looks_like_test_path(u.path):
                test_paths.append(f"{u.repo}/{u.path}")

        findings: list[Finding] = []

        for canonical, group in by_canonical.items():
            if len(group) < 2:
                continue
            langs = {u.language for u in group}
            if len(langs) < 2:
                continue
            repos = sorted({u.repo for u in group})
            names = sorted({simple_name(u.qualified_name) for u in group})
            naming_map = _naming_variants(names[0])
            proto_unit = rpcs.get(canonical)
            has_proto = proto_unit is not None
            langs_with_impl = langs.copy()

            # Surface parity: proto RPC without implementation in every language present
            if has_proto:
                for lang in {"python", "go", "typescript", "javascript"}:
                    if lang in langs and not any(u.language == lang for u in group):
                        langs_with_impl.discard(lang)

            missing_in: list[str] = []
            all_langs = sorted({u.language for u in impls if u.repo in repos})
            for lang in all_langs:
                if not any(u.language == lang and _to_snake(simple_name(u.qualified_name)) == canonical for u in impls):
                    missing_in.append(lang)

            if missing_in or (has_proto and len(group) < len(all_langs)):
                surface_langs = sorted(langs)
                if has_proto:
                    surface_langs = sorted(set(surface_langs) | {"proto"})
                absent = missing_in or [
                    lang
                    for lang in all_langs
                    if not any(u.language == lang for u in group)
                ]
                f = try_make_finding(
                    id=content_hash(f"contract:surface:{canonical}:{','.join(absent)}"),
                    goal=self.goal,
                    sensor=self.id,
                    severity=Severity.HIGH,
                    title=f"Contract surface drift `{canonical}`",
                    evidence=(
                        f"Cross-language cluster ({', '.join(surface_langs)}) lacks implementation "
                        f"in {', '.join(absent)} — e.g. merge_scores in Python+proto but absent in TS."
                        if canonical == "merge_scores"
                        else f"Implementations diverge across {', '.join(repos)}; missing in {', '.join(absent)}."
                    ),
                    unit_ids=[u.id for u in group[:8]] + ([proto_unit.id] if proto_unit else []),
                    action_hint=SuggestedAction.EXTRACT_SHARED,
                    score=0.9,
                    metadata={
                        "canonical": canonical,
                        "naming_map": naming_map,
                        "names": names,
                        "repos": repos,
                        "missing_languages": absent,
                        "has_proto_rpc": has_proto,
                    },
                    known_ids=known,
                )
                if f:
                    findings.append(f)

            contract_ok = any(
                _has_contract_tests(test_paths, repo, canonical) for repo in repos
            )
            if len(group) >= 2 and not contract_ok and not has_proto:
                f = try_make_finding(
                    id=content_hash(f"contract:missing_tests:{canonical}"),
                    goal=self.goal,
                    sensor=self.id,
                    severity=Severity.MEDIUM,
                    title=f"No contract/golden tests for `{canonical}` cluster",
                    evidence=(
                        f"{len(group)} cross-language implementations ({', '.join(names)}) "
                        "share no contract or golden-vector test file."
                    ),
                    unit_ids=[u.id for u in group[:8]],
                    action_hint=SuggestedAction.ENSURE_TESTS,
                    score=0.75,
                    metadata={"canonical": canonical, "naming_map": naming_map},
                    known_ids=known,
                )
                if f:
                    findings.append(f)

        # Explicit merge_scores / NormalizeUserID clusters common in demo workspace
        for special in ("merge_scores", "normalize_user_id"):
            group = by_canonical.get(special, [])
            if len(group) < 2:
                continue
            naming_map = _naming_variants(simple_name(group[0].qualified_name))
            proto_unit = rpcs.get(special.replace("user_id", "user") if special == "normalize_user_id" else special)
            if special == "normalize_user_id":
                proto_unit = rpcs.get("normalize_user") or proto_unit
            if special == "merge_scores":
                ts_has = any(u.language in {"typescript", "javascript"} for u in group)
                py_has = any(u.language == "python" for u in group)
                proto_has = "merge_scores" in rpcs
                if py_has and proto_has and not ts_has:
                    f = try_make_finding(
                        id=content_hash("contract:merge_scores:ts_missing"),
                        goal=self.goal,
                        sensor=self.id,
                        severity=Severity.HIGH,
                        title="merge_scores in Python+proto but absent in TypeScript",
                        evidence=(
                            "Python merge_scores and proto MergeScores RPC exist; "
                            "TypeScript src/normalize.ts exports no mergeScores — contract drift."
                        ),
                        unit_ids=[u.id for u in group if u.language == "python"][:4]
                        + ([rpcs["merge_scores"].id] if "merge_scores" in rpcs else []),
                        action_hint=SuggestedAction.EXTRACT_SHARED,
                        score=0.92,
                        metadata={
                            "canonical": "merge_scores",
                            "naming_map": naming_map,
                        },
                        known_ids=known,
                    )
                    if f and not any(x.metadata.get("canonical") == "merge_scores" for x in findings):
                        findings.append(f)

        return findings


def contract() -> ContractSensor:
    return ContractSensor()
