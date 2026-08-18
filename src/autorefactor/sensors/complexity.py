from __future__ import annotations

import re

from tree_sitter import Language, Node, Parser

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
from autorefactor.sensors.base import AnalysisContext
from autorefactor.sensors.similarity import is_test_path

# Decision points that increase approximate cyclomatic / cognitive complexity
_BRANCH_TYPES = frozenset(
    {
        "if_statement",
        "elif_clause",
        "else_clause",
        "ternary_expression",
        "conditional_expression",  # python
        "case_statement",
        "switch_statement",
        "switch_expression",
        "catch_clause",
        "except_clause",
        "for_statement",
        "for_in_statement",
        "enhanced_for_statement",
        "while_statement",
        "do_statement",
        "binary_expression",  # filtered to && / || below
    }
)

_NEST_TYPES = frozenset(
    {
        "if_statement",
        "for_statement",
        "for_in_statement",
        "enhanced_for_statement",
        "while_statement",
        "do_statement",
        "switch_statement",
        "switch_expression",
        "try_statement",
        "catch_clause",
        "except_clause",
        "function_definition",
        "function_declaration",
        "method_declaration",
        "lambda_expression",
        "anonymous_function",
    }
)


def _parser_for(language: str) -> Parser | None:
    try:
        if language == "python":
            import tree_sitter_python as tsp

            return Parser(Language(tsp.language()))
        if language == "java":
            import tree_sitter_java as tsj

            return Parser(Language(tsj.language()))
        if language in {"typescript", "javascript", "tsx"}:
            import tree_sitter_typescript as tst

            lang = tst.language_tsx() if language == "tsx" else tst.language_typescript()
            return Parser(Language(lang))
        if language == "go":
            import tree_sitter_go as tsg

            return Parser(Language(tsg.language()))
    except Exception:  # noqa: BLE001
        return None
    return None


def _is_logical_bin(node: Node, source: bytes) -> bool:
    if node.type != "binary_expression":
        return False
    text = source[node.start_byte : node.end_byte]
    # crude: look for && or || operators in children
    for child in node.children:
        op = source[child.start_byte : child.end_byte].decode("utf-8", errors="ignore")
        if op in {"&&", "||", "and", "or"}:
            return True
    return b"&&" in text or b"||" in text or b" and " in text or b" or " in text


def measure_complexity(source: str, language: str) -> tuple[int, int]:
    """Return (nesting_depth, cyclomatic_approx) for a function body."""
    nesting, complexity, _catch, _loops = measure_complexity_detail(source, language)
    return nesting, complexity


def measure_complexity_detail(source: str, language: str) -> tuple[int, int, int, int]:
    """Return (nesting_depth, cyclomatic_approx, catch_clause_count, max_nested_loop_depth)."""
    parser = _parser_for(language)
    if parser is None or not source.strip():
        return 0, 1, 0, 0
    data = source.encode("utf-8")
    tree = parser.parse(data)
    max_depth = 0
    complexity = 1
    catch_count = 0
    max_loop_nest = 0
    _LOOP_TYPES = frozenset(
        {
            "for_statement",
            "for_in_statement",
            "enhanced_for_statement",
            "while_statement",
            "do_statement",
        }
    )

    def walk(node: Node, depth: int, loop_depth: int) -> None:
        nonlocal max_depth, complexity, catch_count, max_loop_nest
        max_depth = max(max_depth, depth)
        ntype = node.type
        next_loop = loop_depth + 1 if ntype in _LOOP_TYPES else loop_depth
        if ntype in _LOOP_TYPES:
            max_loop_nest = max(max_loop_nest, next_loop)
        if ntype in {"catch_clause", "except_clause"}:
            catch_count += 1
        if ntype in _BRANCH_TYPES:
            if ntype == "binary_expression":
                if _is_logical_bin(node, data):
                    complexity += 1
            else:
                complexity += 1
        next_depth = depth + 1 if ntype in _NEST_TYPES else depth
        for child in node.children:
            walk(child, next_depth, next_loop)

    walk(tree.root_node, 0, 0)
    return max_depth, complexity, catch_count, max_loop_nest


_DTO_CONVERT_NAME = re.compile(
    r"^(to[A-Z].*|mapTo.*|convertTo.*|fromProto.*|toProto.*|of[A-Z].*)$"
)
_MIN_COMPLEXITY_LOC = 40


class ComplexitySensor:
    """Nesting depth and approximate cyclomatic complexity (first-party)."""

    id = "complexity"
    goal = AnalysisGoal.STRUCTURE

    def available(self, ctx: AnalysisContext) -> bool:
        return True

    def collect(self, ctx: AnalysisContext) -> list[Finding]:
        from autorefactor.index.hierarchy import simple_name

        known = ctx.known_ids()
        max_nest = int(getattr(ctx.config, "max_nesting_depth", 4) or 4)
        max_cx = int(getattr(ctx.config, "max_cognitive_complexity", 15) or 15)
        findings: list[Finding] = []

        for unit in ctx.units:
            if unit.kind != UnitKind.FUNCTION:
                continue
            if is_test_path(unit.path):
                continue
            snippet = unit.source_snippet or ""
            loc = snippet.count("\n") + 1 if snippet else 0
            if loc < _MIN_COMPLEXITY_LOC:
                continue
            name = simple_name(unit.qualified_name)
            if _DTO_CONVERT_NAME.match(name):
                continue
            nesting, complexity, catch_count, nested_loops = measure_complexity_detail(
                snippet, unit.language or "python"
            )
            # Catch-dominated HTTP wrappers: catch clauses drive most of the score
            branch_increments = max(0, complexity - 1)
            if catch_count >= 2 and branch_increments > 0 and catch_count * 2 >= branch_increments:
                continue
            nested_traversal = nested_loops >= 2
            if nesting < max_nest and complexity < max_cx and not nested_traversal:
                continue
            reasons = []
            if nesting >= max_nest:
                reasons.append(f"nesting depth {nesting} (threshold {max_nest})")
            if complexity >= max_cx:
                reasons.append(f"complexity {complexity} (threshold {max_cx})")
            if nested_traversal:
                reasons.append(f"nested_traversal loops={nested_loops}")
            score = min(
                1.0,
                0.5 * (nesting / max(max_nest, 1)) + 0.5 * (complexity / max(max_cx, 1)),
            )
            if nested_traversal:
                score = min(1.0, score + 0.08)
            meta: dict = {"language": unit.language}
            if nested_traversal:
                meta["nested_traversal"] = True
            f = try_make_finding(
                id=content_hash(f"complexity:{unit.id}:{nesting}:{complexity}:{nested_loops}"),
                goal=self.goal,
                sensor=self.id,
                severity=Severity.HIGH if nesting >= max_nest + 2 or complexity >= max_cx * 2 else Severity.MEDIUM,
                title=f"High complexity {unit.qualified_name}",
                evidence="; ".join(reasons),
                unit_ids=[unit.id],
                action_hint=SuggestedAction.SPLIT,
                score=score,
                metrics={
                    "nesting": float(nesting),
                    "complexity": float(complexity),
                    "catch_count": float(catch_count),
                    "nested_loop_depth": float(nested_loops),
                },
                hints=["nested_traversal"] if nested_traversal else [],
                metadata=meta,
                known_ids=known,
            )
            if f:
                findings.append(f)
        return findings
