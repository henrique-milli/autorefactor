from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

AUTOREFACTOR_DOC_MARKER = "autorefactor-doc"
_TODO_RE = re.compile(r"TODO:\s*characterize", re.I)


class DocPack(Protocol):
    language: str
    aliases: tuple[str, ...]

    def system_prompt(self, kind: str) -> str: ...

    def wrap(self, inner: str) -> str: ...

    def extract(self, snippet: str) -> str | None: ...

    def is_replaceable(self, snippet: str) -> bool: ...


def ensure_marker(inner: str) -> str:
    text = (inner or "").strip()
    if not text:
        return AUTOREFACTOR_DOC_MARKER
    if AUTOREFACTOR_DOC_MARKER in text:
        return text
    lines = text.splitlines()
    if lines:
        lines[0] = f"{lines[0]} ({AUTOREFACTOR_DOC_MARKER})"
        return "\n".join(lines)
    return f"{text} ({AUTOREFACTOR_DOC_MARKER})"


def strip_marker_noise(inner: str) -> str:
    return (inner or "").strip()


def common_replaceable(snippet: str, *, has_doc: bool) -> bool:
    """True when missing docs, TODO stub, or autorefactor-owned marker."""
    if not has_doc:
        return True
    if _TODO_RE.search(snippet or ""):
        return True
    if AUTOREFACTOR_DOC_MARKER in (snippet or ""):
        return True
    return False


@dataclass(frozen=True)
class PackMeta:
    language: str
    aliases: tuple[str, ...] = ()
