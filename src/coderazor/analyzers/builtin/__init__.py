from __future__ import annotations

from coderazor.analyzers.base import AnalyzerContext
from coderazor.models import CodeUnit, Flag, FlagSource, Severity, SuggestedAction, UnitKind, content_hash


class LongUnitAnalyzer:
    name = "long_unit"

    def __init__(self, max_lines: int = 80) -> None:
        self.max_lines = max_lines

    def available(self, ctx: AnalyzerContext) -> bool:
        return True

    def analyze(self, ctx: AnalyzerContext) -> list[Flag]:
        flags: list[Flag] = []
        for unit in ctx.units:
            if unit.kind != UnitKind.FUNCTION:
                continue
            lines = unit.span.end_line - unit.span.start_line + 1
            if unit.span.end_line == 0:
                lines = unit.source_snippet.count("\n") + 1
            if lines < self.max_lines:
                continue
            flags.append(
                Flag(
                    id=content_hash(f"long:{unit.id}:{lines}"),
                    source=FlagSource.SMELL,
                    severity=Severity.MEDIUM if lines < self.max_lines * 2 else Severity.HIGH,
                    title=f"Long function {unit.qualified_name}",
                    evidence=f"{lines} lines (threshold {self.max_lines})",
                    unit_ids=[unit.id],
                    suggested_action=SuggestedAction.SPLIT,
                    score=min(1.0, lines / (self.max_lines * 2)),
                    metadata={"lines": lines, "analyzer": self.name},
                )
            )
        return flags


class LargeFileAnalyzer:
    name = "large_file"

    def __init__(self, max_lines: int = 500) -> None:
        self.max_lines = max_lines

    def available(self, ctx: AnalyzerContext) -> bool:
        return True

    def analyze(self, ctx: AnalyzerContext) -> list[Flag]:
        flags: list[Flag] = []
        for unit in ctx.units:
            if unit.kind != UnitKind.FILE:
                continue
            lines = unit.span.end_line or (unit.source_snippet.count("\n") + 1)
            if lines < self.max_lines:
                continue
            flags.append(
                Flag(
                    id=content_hash(f"largefile:{unit.id}:{lines}"),
                    source=FlagSource.SMELL,
                    severity=Severity.MEDIUM if lines < self.max_lines * 2 else Severity.HIGH,
                    title=f"Large file {unit.path}",
                    evidence=f"{lines} lines (threshold {self.max_lines})",
                    unit_ids=[unit.id],
                    suggested_action=SuggestedAction.SPLIT,
                    score=min(1.0, lines / (self.max_lines * 2)),
                    metadata={"lines": lines, "analyzer": self.name},
                )
            )
        return flags


class DuplicateNameAnalyzer:
    name = "duplicate_name"

    def available(self, ctx: AnalyzerContext) -> bool:
        return True

    def analyze(self, ctx: AnalyzerContext) -> list[Flag]:
        from collections import defaultdict

        from coderazor.index.hierarchy import simple_name

        by_name: dict[str, list[CodeUnit]] = defaultdict(list)
        for unit in ctx.units:
            if unit.kind not in {
                UnitKind.FUNCTION,
                UnitKind.TYPE,
                UnitKind.PROTO_MESSAGE,
                UnitKind.PROTO_RPC,
            }:
                continue
            by_name[simple_name(unit.qualified_name)].append(unit)

        flags: list[Flag] = []
        for name, group in by_name.items():
            repos = {u.repo for u in group}
            if len(repos) < 2:
                continue
            flags.append(
                Flag(
                    id=content_hash(f"dupname:{name}:{','.join(sorted(u.id for u in group))}"),
                    source=FlagSource.SMELL,
                    severity=Severity.MEDIUM,
                    title=f"Duplicate name '{name}' across repos",
                    evidence=f"Found in repos: {', '.join(sorted(repos))}",
                    unit_ids=[u.id for u in group],
                    suggested_action=SuggestedAction.EXTRACT_SHARED,
                    score=0.7,
                    metadata={"name": name, "repos": sorted(repos), "analyzer": self.name},
                )
            )
        return flags


class GodModuleAnalyzer:
    name = "god_module"

    def __init__(self, min_children: int = 25) -> None:
        self.min_children = min_children

    def available(self, ctx: AnalyzerContext) -> bool:
        return True

    def analyze(self, ctx: AnalyzerContext) -> list[Flag]:
        from collections import defaultdict

        children: dict[str, list[CodeUnit]] = defaultdict(list)
        by_id = {u.id: u for u in ctx.units}
        for unit in ctx.units:
            if unit.parent_id and unit.kind in {UnitKind.FUNCTION, UnitKind.TYPE}:
                children[unit.parent_id].append(unit)

        flags: list[Flag] = []
        for parent_id, kids in children.items():
            if len(kids) < self.min_children:
                continue
            parent = by_id.get(parent_id)
            if not parent or parent.kind not in {UnitKind.MODULE, UnitKind.FILE, UnitKind.TYPE}:
                continue
            flags.append(
                Flag(
                    id=content_hash(f"god:{parent_id}:{len(kids)}"),
                    source=FlagSource.SMELL,
                    severity=Severity.HIGH if len(kids) > self.min_children * 2 else Severity.MEDIUM,
                    title=f"Oversized module/type {parent.qualified_name}",
                    evidence=f"{len(kids)} child functions/types (threshold {self.min_children})",
                    unit_ids=[parent_id, *[k.id for k in kids[:20]]],
                    suggested_action=SuggestedAction.SPLIT,
                    score=min(1.0, len(kids) / (self.min_children * 2)),
                    metadata={"child_count": len(kids), "analyzer": self.name},
                )
            )
        return flags


class BuiltinAnalyzerBundle:
    """Runs all built-in structural smell analyzers."""

    name = "builtin"

    def available(self, ctx: AnalyzerContext) -> bool:
        return True

    def analyze(self, ctx: AnalyzerContext) -> list[Flag]:
        plugins = [
            LongUnitAnalyzer(ctx.config.long_function_lines),
            LargeFileAnalyzer(ctx.config.large_file_lines),
            DuplicateNameAnalyzer(),
            GodModuleAnalyzer(),
        ]
        flags: list[Flag] = []
        for plugin in plugins:
            flags.extend(plugin.analyze(ctx))
        return flags
