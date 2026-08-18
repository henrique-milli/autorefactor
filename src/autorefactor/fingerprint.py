"""Structural code fingerprints (MOSS-style winnowing).

Inspired by Schleimer/Wilkerson/Aiken (SIGMOD 2003) and modern AI-slop
detectors (dupehound): normalize identifiers/literals, hash k-grams, keep
winnowed fingerprints, score pairs with Jaccard. No embeddings required.
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict

# Shared with similarity.shape_normalize intent
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_STR = re.compile(r"(\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`)")
_NUM = re.compile(r"\b\d+(?:\.\d+)?\b")
_KEYWORDS = frozenset(
    {
        "if",
        "else",
        "for",
        "while",
        "return",
        "class",
        "def",
        "function",
        "public",
        "private",
        "protected",
        "static",
        "void",
        "int",
        "long",
        "string",
        "bool",
        "boolean",
        "true",
        "false",
        "null",
        "none",
        "import",
        "from",
        "package",
        "try",
        "catch",
        "except",
        "finally",
        "throw",
        "throws",
        "new",
        "this",
        "super",
        "const",
        "let",
        "var",
        "async",
        "await",
        "yield",
        "switch",
        "case",
        "break",
        "continue",
        "struct",
        "interface",
        "enum",
        "extends",
        "implements",
        "override",
        "final",
        "abstract",
        "synchronized",
        "volatile",
        "goto",
        "func",
        "type",
        "map",
        "chan",
        "go",
        "defer",
        "select",
        "range",
        "in",
        "is",
        "not",
        "and",
        "or",
        "with",
        "as",
        "lambda",
        "pass",
        "raise",
        "assert",
    }
)


def normalize_tokens(source: str) -> list[str]:
    """Drop comments; map idents→ID, strings→STR, numbers→NUM; keep keywords/punct."""
    text = _STR.sub(" STR ", source)
    text = _NUM.sub(" NUM ", text)
    text = re.sub(r"//.*?$", " ", text, flags=re.MULTILINE)
    text = re.sub(r"#.*?$", " ", text, flags=re.MULTILINE)
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)
    text = re.sub(r'"""[\s\S]*?"""', " STR ", text)
    text = re.sub(r"'''[\s\S]*?'''", " STR ", text)
    tokens: list[str] = []
    for raw in re.findall(r"[A-Za-z_][A-Za-z0-9_]*|[^\s\w]", text):
        if _IDENT.fullmatch(raw):
            tokens.append(raw if raw.lower() in _KEYWORDS else "ID")
        else:
            tokens.append(raw)
    return tokens


def _hash_gram(gram: tuple[str, ...]) -> int:
    h = hashlib.sha1("\0".join(gram).encode("utf-8")).digest()
    return int.from_bytes(h[:8], "big")


def winnow_fingerprints(
    tokens: list[str],
    *,
    k: int = 10,
    window: int = 5,
) -> set[int]:
    """Robust winnowing: guarantee capture of any shared run of k+window-1 tokens."""
    if len(tokens) < k:
        if not tokens:
            return set()
        return {_hash_gram(tuple(tokens))}
    hashes = [_hash_gram(tuple(tokens[i : i + k])) for i in range(len(tokens) - k + 1)]
    if window <= 1 or len(hashes) <= window:
        return set(hashes)
    selected: set[int] = set()
    for i in range(len(hashes) - window + 1):
        window_slice = hashes[i : i + window]
        # Rightmost minimum (stable under shifts)
        min_val = min(window_slice)
        for j in range(window - 1, -1, -1):
            if window_slice[j] == min_val:
                selected.add(min_val)
                break
    return selected


def jaccard(a: set[int], b: set[int]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def fingerprints_for_source(source: str, *, k: int = 10, window: int = 5) -> set[int]:
    return winnow_fingerprints(normalize_tokens(source), k=k, window=window)


def candidate_pairs(
    unit_fps: dict[str, set[int]],
    *,
    min_shared: int = 2,
) -> list[tuple[str, str, float]]:
    """Inverted-index candidate generation → Jaccard scores (no O(n²) full scan)."""
    postings: dict[int, list[str]] = defaultdict(list)
    for uid, fps in unit_fps.items():
        for fp in fps:
            postings[fp].append(uid)

    candidate_counts: dict[tuple[str, str], int] = defaultdict(int)
    for ids in postings.values():
        if len(ids) < 2 or len(ids) > 24:
            # Skip ultra-common boilerplate fingerprints (handler templates, etc.)
            continue
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                a, b = ids[i], ids[j]
                key = (a, b) if a < b else (b, a)
                candidate_counts[key] += 1

    scored: list[tuple[str, str, float]] = []
    for (a, b), shared in candidate_counts.items():
        if shared < min_shared:
            continue
        score = jaccard(unit_fps[a], unit_fps[b])
        scored.append((a, b, score))
    scored.sort(key=lambda t: -t[2])
    return scored
