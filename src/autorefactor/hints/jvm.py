from __future__ import annotations

import re

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
from autorefactor.sensors.similarity import is_test_path

_DOMAIN_RESPONSE = re.compile(
    r"\b(BonusResponse|\w+Response|\w+Dto|\w+DTO)\b",
)
_WIRE_MODEL = re.compile(r"\b(Api\w+|Wire\w+|Transport\w+|Pam\w+)\b")
_CLIENT_REPO = re.compile(r"(Client|Repository)$")
_MAPPING_SERVICE = re.compile(r"\w*MappingService$")


class JvmHintPack:
    """Optional soft hints for JVM role-name / main↔test mirrors. Off by default."""

    id = "jvm"

    def _scan_layer_findings(self, ctx: AnalysisContext, known: set[str]) -> list[Finding]:
        findings: list[Finding] = []
        mapping_types: dict[str, list[tuple[str, str, str]]] = {}

        for unit in ctx.units:
            if unit.kind != UnitKind.TYPE or is_test_path(unit.path):
                continue
            name = simple_name(unit.qualified_name)
            path = unit.path.replace("\\", "/")
            pkg = unit.qualified_name.rsplit(".", 1)[0] if "." in unit.qualified_name else ""

            if _MAPPING_SERVICE.search(name):
                mapping_types.setdefault(name, []).append((unit.id, path, pkg))

            if not (_CLIENT_REPO.search(name) or "Client" in name or "Repository" in name):
                continue
            snippet = unit.source_snippet or ""
            if not _DOMAIN_RESPONSE.search(snippet):
                continue
            if _WIRE_MODEL.search(snippet) and not _DOMAIN_RESPONSE.search(
                re.sub(r"\b(Api|Wire|Transport)\w+\b", "", snippet)
            ):
                continue
            f = try_make_finding(
                id=content_hash(f"jvm:domain_return:{unit.id}"),
                goal=AnalysisGoal.STRUCTURE,
                sensor="jvm",
                severity=Severity.MEDIUM,
                title=f"Client/repository returns domain DTO from `{name}`",
                evidence=(
                    f"`{path}` public methods return domain response types (e.g. BonusResponse) "
                    "instead of wire/API models — introduce transport DTO + mapper at boundary."
                ),
                unit_ids=[unit.id],
                action_hint=SuggestedAction.MOVE,
                score=0.74,
                hints=["layer_boundary", "domain_dto_leak"],
                metadata={
                    "kind": "domain_dto_return",
                    "suggested_layer": "transport",
                    "repo": unit.repo,
                },
                known_ids=known,
            )
            if f:
                findings.append(f)

        for name, entries in mapping_types.items():
            if len(entries) < 2:
                continue
            pkgs = {pkg for _, _, pkg in entries}
            segments = {re.sub(r"\.(mapper|mapping)(\.|$)", ".SEG.", p) for p in pkgs}
            if len(segments) <= 1:
                continue
            mapper_pkgs = [p for _, _, p in entries if ".mapper" in p.replace("\\", "/").lower()]
            mapping_pkgs = [p for _, _, p in entries if ".mapping" in p.replace("\\", "/").lower()]
            if not mapper_pkgs or not mapping_pkgs:
                continue
            ids = [e[0] for e in entries]
            f = try_make_finding(
                id=content_hash(f"jvm:mapper_package:{name}:{','.join(sorted(ids))}"),
                goal=AnalysisGoal.STRUCTURE,
                sensor="jvm",
                severity=Severity.MEDIUM,
                title=f"Package split for `{name}` (mapper vs mapping)",
                evidence=(
                    f"Interface/impl for `{name}` spans `.mapper` and `.mapping` packages — "
                    f"consider consolidating under one package (e.g. `{mapping_pkgs[0]}`)."
                ),
                unit_ids=ids[:4],
                action_hint=SuggestedAction.MOVE,
                score=0.72,
                hints=["layer_boundary", "package_rename"],
                metadata={
                    "kind": "mapper_mapping_split",
                    "simple_name": name,
                    "mapper_packages": mapper_pkgs[:2],
                    "mapping_packages": mapping_pkgs[:2],
                },
                known_ids=known,
            )
            if f:
                findings.append(f)

        return findings

    def annotate(self, findings: list[Finding], ctx: AnalysisContext) -> list[Finding]:
        known = ctx.known_ids()
        extra = self._scan_layer_findings(ctx, known)
        findings = list(findings) + extra

        by_id = {u.id: u for u in ctx.units}
        out: list[Finding] = []
        roles = ("Client", "Service", "Controller", "Repository", "Mapper", "Adapter", "Entity")
        for finding in findings:
            hints = list(finding.hints)
            units = [by_id[uid] for uid in finding.unit_ids if uid in by_id]
            if len(units) >= 2:
                a, b = units[0], units[1]
                na, nb = simple_name(a.qualified_name), simple_name(b.qualified_name)
                for r1 in roles:
                    for r2 in roles:
                        if r1 != r2 and na.endswith(r1) and nb.endswith(r2):
                            if "role_boundary" not in hints:
                                hints.append("role_boundary")
                if is_test_path(a.path) != is_test_path(b.path):
                    if "main_test_mirror" not in hints:
                        hints.append("main_test_mirror")
            out.append(finding.model_copy(update={"hints": hints}) if hints != finding.hints else finding)
        return out


def jvm():
    return JvmHintPack()
