from __future__ import annotations

import difflib
import re
from collections import defaultdict

import numpy as np

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

CLONE_KINDS = {
    UnitKind.FUNCTION,
    UnitKind.TYPE,
    UnitKind.PROTO_MESSAGE,
    UnitKind.PROTO_RPC,
}

LOW_SIGNAL_NAMES = frozenset(
    {
        "setup",
        "teardown",
        "equals",
        "hashcode",
        "tostring",
        "clone",
        "__str__",
        "__repr__",
        "__hash__",
        "__eq__",
        "__init__",
    }
)

_TEST_MARKERS = (
    "/src/test/",
    "/src/tests/",
    "/__tests__/",
    "/test/",
    "/tests/",
    "_test.go",
    ".test.ts",
    ".test.js",
    ".spec.ts",
    ".spec.js",
)

_IDENT = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")
_STR = re.compile(r'"(?:\\.|[^"\\])*"')
_NUM = re.compile(r"\b\d+\b")
_KEYWORDS = frozenset(
    {
        "if",
        "else",
        "for",
        "while",
        "return",
        "new",
        "void",
        "public",
        "private",
        "protected",
        "static",
        "class",
        "null",
        "true",
        "false",
        "try",
        "catch",
        "throw",
        "throws",
        "this",
        "super",
        "final",
        "abstract",
        "boolean",
        "int",
        "long",
        "double",
        "float",
        "byte",
        "short",
        "char",
        "break",
        "continue",
        "switch",
        "case",
        "default",
        "do",
        "var",
        "instanceof",
        "import",
        "package",
        "interface",
        "enum",
        "extends",
        "implements",
        "override",
        "async",
        "await",
        "def",
        "None",
        "True",
        "False",
        "pass",
        "raise",
        "except",
        "finally",
        "with",
        "yield",
        "lambda",
        "from",
        "as",
        "in",
        "not",
        "and",
        "or",
        "is",
        "func",
        "type",
        "struct",
        "map",
        "chan",
        "go",
        "defer",
        "select",
        "range",
        "const",
        "let",
        "fn",
        "mut",
        "impl",
        "pub",
        "mod",
        "use",
        "crate",
        "self",
        "Self",
        "match",
        "loop",
        "where",
        "trait",
        "dyn",
        "move",
        "ref",
        "sizeof",
        "typeof",
        "nil",
        "undefined",
        "function",
        "constructor",
        "export",
        "readonly",
        "keyof",
        "infer",
        "never",
        "unknown",
        "any",
        "string",
        "number",
        "object",
        "symbol",
        "bigint",
    }
)


def is_test_path(path: str) -> bool:
    lower = path.replace("\\", "/").lower()
    if any(m in lower for m in _TEST_MARKERS):
        return True
    base = path.rsplit("/", 1)[-1]
    return bool(re.search(r"Test\.(java|kt)$", base) or re.search(r"_test\.py$", base))


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denom == 0:
        return 0.0
    return float(np.dot(a, b) / denom)


def shape_normalize(source: str) -> str:
    """Identifier/literal-normalized source for structural clone detection."""
    text = _STR.sub("STR", source)
    text = _NUM.sub("NUM", text)
    text = re.sub(r"//.*?$", "", text, flags=re.MULTILINE)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    parts: list[str] = []
    last = 0
    for m in _IDENT.finditer(text):
        parts.append(text[last : m.start()])
        word = m.group()
        parts.append(word if word in _KEYWORDS else "ID")
        last = m.end()
    parts.append(text[last:])
    return " ".join("".join(parts).split())


def shape_similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, shape_normalize(a), shape_normalize(b)).ratio()


def domain_token_divergence(a_src: str, b_src: str) -> float:
    """Jaccard distance of non-keyword identifiers (1 = fully divergent domain vocab)."""

    def tokens(src: str) -> set[str]:
        out: set[str] = set()
        for m in _IDENT.finditer(src or ""):
            t = m.group(0)
            if t in _KEYWORDS or len(t) < 3:
                continue
            out.add(t.lower())
        return out

    a = tokens(a_src)
    b = tokens(b_src)
    if not a and not b:
        return 0.0
    if not a or not b:
        return 1.0
    inter = len(a & b)
    union = len(a | b)
    if union == 0:
        return 0.0
    return 1.0 - (inter / union)


_DOMAIN_DIVERGENCE_SKIP = 0.45
_DOMAIN_OPPOSITE_TOKEN_PAIRS = (
    ("credit", "reversal"),
    ("debit", "credit"),
    ("deposit", "withdraw"),
    ("request", "response"),
)
_DOMAIN_OPPOSITE_TOKEN = re.compile(r"[a-z]+")


def _type_simple_name(unit: CodeUnit) -> str:
    qn = unit.qualified_name or ""
    if unit.kind == UnitKind.FUNCTION and "." in qn:
        return qn.rsplit(".", 1)[0].rsplit(".", 1)[-1]
    return simple_name(qn)


def _domain_opposite_name_pair(a: CodeUnit, b: CodeUnit) -> bool:
    """True for sibling handlers/types with opposite business roles (Credit vs Reversal, etc.)."""
    na = _type_simple_name(a).lower()
    nb = _type_simple_name(b).lower()
    if na == nb:
        return False
    if "reversal" in na or "reversal" in nb:
        strip_a = na.replace("reversal", "")
        strip_b = nb.replace("reversal", "")
        if strip_a == strip_b or difflib.SequenceMatcher(None, strip_a, strip_b).ratio() >= 0.85:
            return True
    tokens_a = set(_DOMAIN_OPPOSITE_TOKEN.findall(na))
    tokens_b = set(_DOMAIN_OPPOSITE_TOKEN.findall(nb))
    for left, right in _DOMAIN_OPPOSITE_TOKEN_PAIRS:
        if (left in na and right in nb) or (right in na and left in nb):
            return True
        if (left in tokens_a and right in tokens_b) or (right in tokens_a and left in tokens_b):
            return True
    return False

_RETURN_STMT = re.compile(
    r"\breturn\b(?!\s+(?:null|None|void)\s*;)\s*([^;{]+);",
    re.MULTILINE | re.DOTALL,
)
_EXCEPTION_HANDLER = re.compile(r"@ExceptionHandler\s*\(\s*([^)]+)\)")


def _normalize_return_expr(expr: str) -> str:
    return " ".join((expr or "").split()).lower()


def _return_expressions(source: str) -> frozenset[str]:
    """Normalized return expressions (literals/constants preserved before shape norm)."""
    out: set[str] = set()
    for m in _RETURN_STMT.finditer(source or ""):
        expr = _normalize_return_expr(m.group(1))
        if expr and expr not in {"null", "none", "void"}:
            out.add(expr)
    return frozenset(out)


def return_semantics_differ(a_src: str, b_src: str) -> bool:
    """True when both units return and the normalized return expressions differ."""
    a = _return_expressions(a_src)
    b = _return_expressions(b_src)
    if not a or not b:
        return False
    return a != b


def _exception_handler_types(source: str) -> frozenset[str]:
    types: set[str] = set()
    for match in _EXCEPTION_HANDLER.finditer(source or ""):
        inner = match.group(1)
        for part in re.findall(r"(\w+(?:\.\w+)?)\.class", inner):
            types.add(part.rsplit(".", 1)[-1])
    return frozenset(types)


def _distinct_exception_handler_pair(a: CodeUnit, b: CodeUnit) -> bool:
    """True when both are @ExceptionHandler methods with different exception mappings."""
    ta = _exception_handler_types(a.source_snippet or "")
    tb = _exception_handler_types(b.source_snippet or "")
    return bool(ta and tb and ta != tb)


def _is_constructor(unit: CodeUnit) -> bool:
    if unit.kind != UnitKind.FUNCTION:
        return False
    name = simple_name(unit.qualified_name)
    parent = unit.qualified_name.rsplit(".", 1)[0] if "." in unit.qualified_name else ""
    return bool(parent) and name == simple_name(parent)


def _is_low_signal(unit: CodeUnit) -> bool:
    name = simple_name(unit.qualified_name)
    if name.lower() in LOW_SIGNAL_NAMES:
        return True
    if _is_constructor(unit):
        return True
    if re.match(r"^findBy[A-Z]", name):
        return True
    snippet = unit.source_snippet.strip()
    if len(snippet) < 80:
        return True
    return bool(re.match(r"^(get|set|is)[A-Z]", name) and len(snippet) < 120)


def _loc(unit: CodeUnit) -> int:
    if unit.span.end_line and unit.span.start_line:
        return max(1, unit.span.end_line - unit.span.start_line + 1)
    return max(1, unit.source_snippet.count("\n") + 1)


def _is_dto_path(path: str) -> bool:
    lower = path.replace("\\", "/").lower()
    return "/dto/" in lower or lower.startswith("dto/") or "/model/" in lower or "/models/" in lower


def _cosine_matrix(embeddings: np.ndarray) -> np.ndarray:
    """Row-normalized cosine similarity matrix (n×n)."""
    norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
    normalized = embeddings / np.clip(norms, 1e-12, None)
    return normalized @ normalized.T


def _similarity_candidate_indices(
    group: list[CodeUnit],
    sims: np.ndarray,
    *,
    soft_threshold: float,
    cross_repo_only: bool,
    max_candidates: int,
) -> list[tuple[int, int, float]]:
    """Select pair indices for expensive source/shape scoring.

    Avoids full O(n²) SequenceMatcher on large workspaces by:
    - keeping cosine neighbors above ``soft_threshold``
    - keeping a bounded set of same-file pairs (shape clones can have weak embeddings)
    - capping the cosine-neighbor set before ranking
    """
    n = len(group)
    same_path_pairs: list[tuple[int, int, float]] = []
    cosine_pairs: list[tuple[int, int, float]] = []
    by_path: dict[tuple[str, str], list[int]] = defaultdict(list)
    locs = [_loc(unit) for unit in group]
    for idx, unit in enumerate(group):
        by_path[(unit.repo, unit.path)].append(idx)

    max_same_path_per_file = 40
    max_same_path_total = max(200, max_candidates // 2)
    for indices in by_path.values():
        if len(indices) < 2:
            continue
        file_pairs: list[tuple[int, int, float, float]] = []
        for ai, a_idx in enumerate(indices):
            for b_idx in indices[ai + 1 :]:
                a, b = group[a_idx], group[b_idx]
                if cross_repo_only and a.repo == b.repo:
                    continue
                if is_test_path(a.path) != is_test_path(b.path):
                    continue
                la, lb = locs[a_idx], locs[b_idx]
                if min(la, lb) < 15:
                    continue
                # Cheap prefilter: similar length methods only
                if max(la, lb) > 0 and abs(la - lb) / max(la, lb) > 0.55:
                    continue
                name_a = simple_name(a.qualified_name).lower()
                name_b = simple_name(b.qualified_name).lower()
                name_hint = difflib.SequenceMatcher(None, name_a, name_b).ratio()
                if name_hint < 0.35 and abs(la - lb) > 8:
                    continue
                score = float(sims[a_idx, b_idx])
                # Prefer higher cosine + name overlap when capping per file
                file_pairs.append((a_idx, b_idx, score, score + name_hint))
        file_pairs.sort(key=lambda t: -t[3])
        for a_idx, b_idx, score, _rank in file_pairs[:max_same_path_per_file]:
            same_path_pairs.append((a_idx, b_idx, score))
    same_path_pairs.sort(key=lambda t: -t[2])
    if len(same_path_pairs) > max_same_path_total:
        same_path_pairs = same_path_pairs[:max_same_path_total]

    # Upper-triangle neighbors above soft threshold (skip ultra-large dense blocks
    # by taking top rows via argpartition when n is large).
    if n <= 900:
        ii, jj = np.triu_indices(n, k=1)
        scores = sims[ii, jj]
        mask = scores >= soft_threshold
        for a_idx, b_idx, score in zip(
            ii[mask].tolist(), jj[mask].tolist(), scores[mask].tolist(), strict=True
        ):
            a, b = group[a_idx], group[b_idx]
            if cross_repo_only and a.repo == b.repo:
                continue
            if is_test_path(a.path) != is_test_path(b.path):
                continue
            if a.path == b.path and a.repo == b.repo:
                continue  # already covered by same_path_pairs
            cosine_pairs.append((a_idx, b_idx, float(score)))
    else:
        # Large n: per-row top-k above soft_threshold (O(n·k) candidates).
        top_k = min(48, n - 1)
        for i in range(n):
            row = sims[i]
            # Only compare j > i to keep undirected pairs once.
            if i >= n - 1:
                continue
            tail = row[i + 1 :]
            if tail.size == 0:
                continue
            k = min(top_k, tail.size)
            # argpartition for top-k scores in the j>i slice
            part = np.argpartition(tail, -k)[-k:]
            for off in part.tolist():
                score = float(tail[off])
                if score < soft_threshold:
                    continue
                j = i + 1 + off
                a, b = group[i], group[j]
                if cross_repo_only and a.repo == b.repo:
                    continue
                if is_test_path(a.path) != is_test_path(b.path):
                    continue
                if a.path == b.path and a.repo == b.repo:
                    continue
                cosine_pairs.append((i, j, score))

    cosine_pairs.sort(key=lambda t: -t[2])
    if len(cosine_pairs) > max_candidates:
        cosine_pairs = cosine_pairs[:max_candidates]

    # Prefer same-path pairs, then capped cosine neighbors (dedupe).
    seen: set[tuple[int, int]] = set()
    out: list[tuple[int, int, float]] = []
    for a_idx, b_idx, score in same_path_pairs + cosine_pairs:
        key = (a_idx, b_idx) if a_idx < b_idx else (b_idx, a_idx)
        if key in seen:
            continue
        seen.add(key)
        out.append((a_idx, b_idx, score))
    return out


def emit_similarity_findings(
    units: list[CodeUnit],
    *,
    kinds: set[UnitKind],
    goal: AnalysisGoal,
    sensor: str,
    threshold: float,
    max_pairs: int,
    cross_repo_only: bool = False,
    clones_only: bool = False,
    include_tests: bool = False,
    skip_dto_paths: bool = True,
) -> list[Finding]:
    by_kind: dict[UnitKind, list[CodeUnit]] = defaultdict(list)
    for unit in units:
        if unit.kind not in kinds or not unit.embedding or _is_low_signal(unit):
            continue
        if not include_tests and is_test_path(unit.path):
            continue
        if skip_dto_paths and _is_dto_path(unit.path):
            continue
        by_kind[unit.kind].append(unit)

    known = {u.id for u in units}
    findings: list[Finding] = []
    delete_source, delete_name, delete_cos = 0.95, 0.80, 0.85
    extract_cos, extract_name = 0.90, 0.70
    soft_threshold = threshold - 0.05
    # Cap SequenceMatcher work: enough headroom above max_pairs, bounded for 10k+ units.
    max_candidates = max(500, int(max_pairs) * 40)

    for kind, group in by_kind.items():
        if len(group) < 2:
            continue
        matrix = np.array([u.embedding for u in group], dtype=np.float64)
        n = len(group)
        if n > 1500:
            # Avoid materializing a full n×n float64 matrix (~n²·8 bytes).
            norms = np.linalg.norm(matrix, axis=1, keepdims=True)
            normalized = matrix / np.clip(norms, 1e-12, None)
            sims = np.zeros((n, n), dtype=np.float32)
            block = 256
            for start in range(0, n, block):
                end = min(n, start + block)
                sims[start:end] = normalized[start:end] @ normalized.T
        else:
            sims = _cosine_matrix(matrix)
        candidates = _similarity_candidate_indices(
            group,
            sims,
            soft_threshold=soft_threshold,
            cross_repo_only=cross_repo_only,
            max_candidates=max_candidates,
        )
        scored: list[tuple] = []
        for i, j, sim_full in candidates:
            a, b = group[i], group[j]
            cross = a.repo != b.repo
            same_path = a.path == b.path and a.repo == b.repo
            sim = float(sim_full)
            sim_code = 0.0
            code_a = (a.embeddings or {}).get("code")
            code_b = (b.embeddings or {}).get("code")
            if code_a and code_b and len(code_a) == len(code_b):
                sim_code = float(
                    cosine(
                        np.array(code_a, dtype=np.float64),
                        np.array(code_b, dtype=np.float64),
                    )
                )
                # Light boost when code-view agrees with primary (full) match
                if sim_code >= threshold - 0.02:
                    sim = min(1.0, sim + 0.04)
            # Same-file pairs may still be structural clones with mediocre embeddings
            if sim < soft_threshold and not same_path:
                continue
            name_sim = difflib.SequenceMatcher(
                None,
                simple_name(a.qualified_name).lower(),
                simple_name(b.qualified_name).lower(),
            ).ratio()
            source_sim = difflib.SequenceMatcher(
                None,
                " ".join(a.source_snippet.split()),
                " ".join(b.source_snippet.split()),
            ).ratio()
            shape_sim = 0.0
            if same_path and min(_loc(a), _loc(b)) >= 15:
                shape_sim = shape_similarity(a.source_snippet, b.source_snippet)
                if sim < soft_threshold and shape_sim < 0.65:
                    continue
            elif sim < soft_threshold:
                continue
            rank = max(source_sim, shape_sim) * 2 + name_sim + sim + (0.1 if cross else 0.0)
            scored.append(
                (rank, sim, float(sim_full), sim_code, source_sim, name_sim, shape_sim, i, j, cross, same_path)
            )
        scored.sort(reverse=True)
        kept = 0
        for (
            rank,
            sim,
            sim_full,
            sim_code,
            source_sim,
            name_sim,
            shape_sim,
            i,
            j,
            cross,
            same_path,
        ) in scored:
            if kept >= max_pairs:
                break
            a, b = group[i], group[j]
            if a.repo == b.repo and a.qualified_name == b.qualified_name:
                continue
            distinct_handlers = _distinct_exception_handler_pair(a, b)
            if not distinct_handlers and return_semantics_differ(
                a.source_snippet or "", b.source_snippet or ""
            ):
                continue
            can_delete = (
                kind in CLONE_KINDS
                and source_sim >= delete_source
                and name_sim >= delete_name
                and sim >= delete_cos
                and a.path != b.path
                and not distinct_handlers
            )
            can_extract = cross and not clones_only and sim >= extract_cos and name_sim >= extract_name
            can_merge = False
            can_shape_extract = False
            if same_path and not cross and not clones_only and not can_delete:
                can_merge = (
                    source_sim >= 0.75
                    and sim >= 0.85
                    and name_sim >= 0.55
                    and not distinct_handlers
                )
                # Structural near-clones: renamed identifiers but same control/flow skeleton.
                # Same parent type may qualify on shape alone (embeddings can diverge on literals).
                min_lines = min(_loc(a), _loc(b))
                same_parent = bool(a.parent_id and a.parent_id == b.parent_id)
                can_shape_extract = (
                    not can_merge
                    and shape_sim >= 0.65
                    and name_sim >= 0.45
                    and min_lines >= 15
                    and (sim >= 0.82 or (same_parent and shape_sim >= 0.65))
                ) or distinct_handlers
            elif not same_path:
                can_merge = (
                    not cross
                    and not clones_only
                    and sim >= extract_cos
                    and name_sim >= extract_name
                    and not can_delete
                    and not distinct_handlers
                )
            if distinct_handlers:
                can_shape_extract = True
                can_delete = False
                can_merge = False
            if can_delete:
                action = SuggestedAction.DELETE_DUPLICATE
                title = f"Near-copy {kind.value}: {a.qualified_name} ≈ {b.qualified_name}"
            elif can_extract:
                action = SuggestedAction.EXTRACT_SHARED
                title = f"Cross-repo similar {kind.value}: {a.qualified_name} ≈ {b.qualified_name}"
            elif can_shape_extract:
                action = SuggestedAction.EXTRACT_SHARED
                if distinct_handlers:
                    title = (
                        f"Shared @ExceptionHandler helper: "
                        f"{a.qualified_name} ≈ {b.qualified_name}"
                    )
                else:
                    title = f"Structural clone {kind.value}: {a.qualified_name} ≈ {b.qualified_name}"
            elif can_merge:
                if _domain_opposite_name_pair(a, b):
                    continue
                action = SuggestedAction.MERGE
                title = f"Similar {kind.value}: {a.qualified_name} ≈ {b.qualified_name}"
            else:
                continue
            if (
                action == SuggestedAction.EXTRACT_SHARED
                and min(_loc(a), _loc(b)) >= 15
                and not distinct_handlers
            ):
                div = domain_token_divergence(a.source_snippet or "", b.source_snippet or "")
                if div >= _DOMAIN_DIVERGENCE_SKIP:
                    continue
            finding = try_make_finding(
                id=content_hash(f"sim:{goal.value}:{a.id}:{b.id}"),
                goal=goal,
                sensor=sensor,
                severity=Severity.HIGH if source_sim >= delete_source or sim >= 0.92 else Severity.MEDIUM,
                title=title,
                evidence=(
                    f"kind={kind.value} cosine={sim:.3f} full={sim_full:.3f} "
                    f"code={sim_code:.3f} name={name_sim:.3f} "
                    f"source={source_sim:.3f} shape={shape_sim:.3f} repos={a.repo},{b.repo}"
                    + (
                        " Distinct @ExceptionHandler mappings must be preserved — "
                        "extract a shared helper (e.g. buildClientErrorResponse); "
                        "do not delete or merge either handler unless one path is unreachable."
                        if distinct_handlers
                        else ""
                    )
                ),
                unit_ids=[a.id, b.id],
                action_hint=action,
                score=max(sim, source_sim, shape_sim),
                metrics={
                    "cosine": sim,
                    "sim_full": sim_full,
                    "sim_code": sim_code,
                    "name_similarity": name_sim,
                    "source_similarity": source_sim,
                    "shape_similarity": shape_sim,
                    "cross_repo": 1.0 if cross else 0.0,
                    "removable_loc": float(min(_loc(a), _loc(b)) if can_delete or can_shape_extract else 0),
                },
                known_ids=known,
            )
            if finding is not None:
                findings.append(finding)
                kept += 1
    return findings


_MERGE_METHOD = re.compile(
    r"^(merge|mapTo\w*(?:Response|Aggregation))\w*$",
    re.IGNORECASE,
)
_MERGE_CALL = re.compile(
    r"\b(merge\w+|mapTo\w*(?:Response|Aggregation)\w*)\s*\(",
    re.IGNORECASE,
)
_ENTRY_POINT = re.compile(r"^(get|retrieve|fetch|load|find|process|handle)\w+", re.IGNORECASE)


def _merge_candidates(units: list[CodeUnit]) -> dict[str, list[CodeUnit]]:
    """Group merge/mapTo* methods by parent TYPE id."""
    by_parent: dict[str, list[CodeUnit]] = defaultdict(list)
    for unit in units:
        if unit.kind != UnitKind.FUNCTION or is_test_path(unit.path):
            continue
        name = simple_name(unit.qualified_name)
        if not _MERGE_METHOD.match(name):
            continue
        if _loc(unit) < 3:
            continue
        parent = unit.parent_id or unit.path
        by_parent[parent].append(unit)
    return by_parent


def _merge_api_calls(source: str) -> frozenset[str]:
    calls: set[str] = set()
    for match in _MERGE_CALL.finditer(source or ""):
        name = match.group(1)
        if _MERGE_METHOD.match(name):
            calls.add(name)
    return frozenset(calls)


def _is_public_service_entry(unit: CodeUnit) -> bool:
    if unit.kind != UnitKind.FUNCTION or is_test_path(unit.path):
        return False
    name = simple_name(unit.qualified_name)
    if name.lower() in LOW_SIGNAL_NAMES or _is_low_signal(unit):
        return False
    return bool(_ENTRY_POINT.match(name))


def _callers_of(
    units: list[CodeUnit],
    symbol: str,
    *,
    file_path: str | None = None,
    parent_id: str | None = None,
    repo: str | None = None,
    cross_file: bool = False,
) -> list[str]:
    callers: list[str] = []
    norm_path = file_path.replace("\\", "/") if file_path else None
    for unit in units:
        if unit.kind == UnitKind.TYPE and parent_id and unit.id == parent_id:
            snippet = unit.source_snippet or ""
            for m in re.finditer(
                rf"(?:[\w<>,\[\]\s]+\s+)?(\w+)\s*\([^)]*\)\s*\{{[^{{}}]*\b{re.escape(symbol)}\s*\(",
                snippet,
                re.DOTALL,
            ):
                callers.append(m.group(1))
            continue
        if unit.kind != UnitKind.FUNCTION or is_test_path(unit.path):
            continue
        if repo and unit.repo != repo:
            continue
        unit_path = unit.path.replace("\\", "/")
        if norm_path and unit_path != norm_path and not cross_file:
            continue
        snippet = unit.source_snippet or ""
        if re.search(rf"\b{re.escape(symbol)}\s*\(", snippet):
            caller = simple_name(unit.qualified_name)
            if caller != symbol:
                callers.append(caller)
    return callers


def emit_divergent_merge_findings(
    units: list[CodeUnit],
    *,
    goal: AnalysisGoal,
    sensor: str,
) -> list[Finding]:
    known = {u.id for u in units}
    findings: list[Finding] = []
    by_parent = _merge_candidates(units)

    for _parent_key, group in by_parent.items():
        if len(group) < 2:
            continue
        similar_pairs: list[tuple[CodeUnit, CodeUnit, float]] = []
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a, b = group[i], group[j]
                sim = shape_similarity(a.source_snippet or "", b.source_snippet or "")
                if sim >= 0.75:
                    similar_pairs.append((a, b, sim))
        if not similar_pairs:
            continue

        entry_map: dict[str, set[str]] = defaultdict(set)
        file_path = group[0].path
        parent_id = group[0].parent_id
        repo = group[0].repo
        for unit in group:
            sym = simple_name(unit.qualified_name)
            for caller in _callers_of(
                units,
                sym,
                file_path=file_path,
                parent_id=parent_id,
                repo=repo,
                cross_file=True,
            ):
                entry_map[caller].add(sym)

        distinct_entries = [ep for ep, methods in entry_map.items() if len(methods) == 1]
        if len(distinct_entries) < 2:
            for a, b, sim in similar_pairs:
                callers_a = set(
                    _callers_of(
                        units,
                        simple_name(a.qualified_name),
                        file_path=file_path,
                        parent_id=parent_id,
                        repo=repo,
                        cross_file=True,
                    )
                )
                callers_b = set(
                    _callers_of(
                        units,
                        simple_name(b.qualified_name),
                        file_path=file_path,
                        parent_id=parent_id,
                        repo=repo,
                        cross_file=True,
                    )
                )
                if callers_a and callers_b and not (callers_a & callers_b):
                    na, nb = simple_name(a.qualified_name), simple_name(b.qualified_name)
                    finding = try_make_finding(
                        id=content_hash(f"sim:divergent_merge:{a.id}:{b.id}"),
                        goal=goal,
                        sensor=sensor,
                        severity=Severity.HIGH,
                        title=f"Divergent merge paths: `{na}` vs `{nb}`",
                        evidence=(
                            f"Same TYPE hosts `{na}` and `{nb}` with shape similarity {sim:.2f} but "
                            f"distinct entry points ({', '.join(sorted(callers_a)[:2])} vs "
                            f"{', '.join(sorted(callers_b)[:2])}) — unify on one canonical merge API."
                        ),
                        unit_ids=[a.id, b.id],
                        action_hint=SuggestedAction.MERGE,
                        score=max(sim, 0.85),
                        metrics={"shape_similarity": sim},
                        metadata={
                            "kind": "divergent_merge_paths",
                            "methods": [na, nb],
                            "entry_points": sorted(callers_a | callers_b),
                        },
                        known_ids=known,
                    )
                    if finding:
                        findings.append(finding)
            continue

        a, b, sim = max(similar_pairs, key=lambda t: t[2])
        na, nb = simple_name(a.qualified_name), simple_name(b.qualified_name)
        finding = try_make_finding(
            id=content_hash(f"sim:divergent_merge:{a.id}:{b.id}"),
            goal=goal,
            sensor=sensor,
            severity=Severity.HIGH,
            title=f"Divergent merge paths: `{na}` vs `{nb}`",
            evidence=(
                f"Same TYPE hosts `{na}` and `{nb}` (shape similarity {sim:.2f}) referenced from "
                f"distinct service entry points — unify append vs sum semantics on one merge API."
            ),
            unit_ids=[a.id, b.id],
            action_hint=SuggestedAction.MERGE,
            score=max(sim, 0.85),
            metrics={"shape_similarity": sim},
            metadata={"kind": "divergent_merge_paths", "methods": [na, nb]},
            known_ids=known,
        )
        if finding:
            findings.append(finding)

    return findings


def emit_service_flow_merge_findings(
    units: list[CodeUnit],
    *,
    goal: AnalysisGoal,
    sensor: str,
) -> list[Finding]:
    """Link public service flows that call distinct merge/mapper APIs (cross-method equivalence)."""
    known = {u.id for u in units}
    findings: list[Finding] = []
    merge_by_name: dict[str, CodeUnit] = {}
    for unit in units:
        if unit.kind != UnitKind.FUNCTION or is_test_path(unit.path):
            continue
        name = simple_name(unit.qualified_name)
        if _MERGE_METHOD.match(name):
            merge_by_name[name] = unit

    by_service: dict[str, list[tuple[CodeUnit, frozenset[str]]]] = defaultdict(list)
    for unit in units:
        if not _is_public_service_entry(unit):
            continue
        apis = _merge_api_calls(unit.source_snippet or "")
        if not apis:
            continue
        parent = unit.parent_id or unit.path
        by_service[parent].append((unit, apis))

    for _parent_key, entries in by_service.items():
        if len(entries) < 2:
            continue
        for i in range(len(entries)):
            for j in range(i + 1, len(entries)):
                ua, apis_a = entries[i]
                ub, apis_b = entries[j]
                if apis_a == apis_b:
                    continue
                diff = (apis_a - apis_b) | (apis_b - apis_a)
                if not diff:
                    continue
                merge_refs = [merge_by_name[n] for n in diff if n in merge_by_name]
                if not merge_refs:
                    continue
                na, nb = simple_name(ua.qualified_name), simple_name(ub.qualified_name)
                ma, mb = ", ".join(sorted(apis_a)), ", ".join(sorted(apis_b))
                unit_ids = [ua.id, ub.id] + [u.id for u in merge_refs[:2]]
                finding = try_make_finding(
                    id=content_hash(f"sim:service_flow_merge:{ua.id}:{ub.id}"),
                    goal=goal,
                    sensor=sensor,
                    severity=Severity.HIGH,
                    title=f"Dual aggregation paths: `{na}` vs `{nb}`",
                    evidence=(
                        f"Public service flows `{na}` ({ma}) and `{nb}` ({mb}) call distinct "
                        f"merge/mapper APIs for the same PAM+PokerStars aggregation — "
                        f"inconsistent-totals risk if paths diverge; unify on one canonical merge API."
                    ),
                    unit_ids=unit_ids,
                    action_hint=SuggestedAction.MERGE,
                    score=0.88,
                    metrics={"merge_api_divergence": float(len(diff))},
                    metadata={
                        "kind": "service_flow_merge",
                        "entry_points": [na, nb],
                        "merge_apis": sorted(apis_a | apis_b),
                    },
                    known_ids=known,
                )
                if finding:
                    findings.append(finding)
    return findings


class MethodSimilaritySensor:
    id = "sim.method"
    goal = AnalysisGoal.METHOD_SIMILARITY

    def available(self, ctx: AnalysisContext) -> bool:
        return any(u.kind == UnitKind.FUNCTION and u.embedding for u in ctx.units)

    def collect(self, ctx: AnalysisContext) -> list[Finding]:
        threshold = 0.88 if ctx.focus == "strict" else ctx.config.similarity_threshold
        units = ctx.units_list()
        findings = emit_similarity_findings(
            units,
            kinds={UnitKind.FUNCTION, UnitKind.PROTO_RPC},
            goal=self.goal,
            sensor=self.id,
            threshold=threshold,
            max_pairs=ctx.config.max_similarity_pairs,
            cross_repo_only=(ctx.focus == "cross_repo"),
            clones_only=(ctx.focus == "clones"),
            include_tests=(ctx.focus == "clones"),
        )
        findings.extend(
            emit_divergent_merge_findings(units, goal=self.goal, sensor=self.id)
        )
        findings.extend(
            emit_service_flow_merge_findings(units, goal=self.goal, sensor=self.id)
        )
        return findings


class ClassSimilaritySensor:
    id = "sim.class"
    goal = AnalysisGoal.CLASS_SIMILARITY

    def available(self, ctx: AnalysisContext) -> bool:
        return any(u.kind == UnitKind.TYPE and u.embedding for u in ctx.units)

    def collect(self, ctx: AnalysisContext) -> list[Finding]:
        threshold = 0.88 if ctx.focus == "strict" else ctx.config.similarity_threshold
        return emit_similarity_findings(
            ctx.units_list(),
            kinds={UnitKind.TYPE, UnitKind.PROTO_MESSAGE, UnitKind.PROTO_SERVICE},
            goal=self.goal,
            sensor=self.id,
            threshold=threshold,
            max_pairs=ctx.config.max_similarity_pairs,
            cross_repo_only=(ctx.focus == "cross_repo"),
            clones_only=(ctx.focus == "clones"),
            include_tests=(ctx.focus == "clones"),
        )
