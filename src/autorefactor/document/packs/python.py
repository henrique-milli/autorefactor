from __future__ import annotations

import re

from autorefactor.document.packs.base import (
    AUTOREFACTOR_DOC_MARKER,
    common_replaceable,
    ensure_marker,
    strip_marker_noise,
)

_TRIPLE = re.compile(
    r'''(?:^[^\n]*:\s*\n)?[ \t]*(?:"""|\'\'\')([\s\S]*?)(?:"""|\'\'\')''',
    re.M,
)


class PythonPack:
    language = "python"
    aliases: tuple[str, ...] = ()

    def system_prompt(self, kind: str) -> str:
        depth = (
            "Prefer a one-line summary; add Args/Returns sections only for non-trivial callables. "
            if kind in {"function", "method"}
            else "Summarize the class responsibility briefly. "
        )
        return (
            "You write Python docstrings for a code unit. "
            "Respond with valid JSON only: "
            '{"doc": "inner docstring body without triple quotes", "tags": ["tag1"]}. '
            f"{depth}"
            "Focus on behavior, inputs/outputs, and side effects. "
            f"Include the token {AUTOREFACTOR_DOC_MARKER} once in the first line of doc. "
            "Do not wrap doc in triple quotes."
        )

    def wrap(self, inner: str) -> str:
        body = ensure_marker(strip_marker_noise(inner))
        # Inserted after signature with {INDENT}
        if "\n" not in body:
            return f'{{INDENT}}"""{body}"""\n'
        indented = "\n".join(f"{{INDENT}}{line}" if line else "" for line in body.splitlines())
        return f'{{INDENT}}"""\n{indented}\n{{INDENT}}"""\n'

    def extract(self, snippet: str) -> str | None:
        text = snippet or ""
        # Prefer docstring immediately after def/class header
        m = re.search(
            r'''(?:def|class)\s+\w+[\s\S]*?:\s*(?:"""|\'\'\')([\s\S]*?)(?:"""|\'\'\')''',
            text[:2500],
        )
        if not m:
            m = _TRIPLE.search(text[:2500])
        if not m:
            return None
        inner = m.group(1).strip("\n")
        # Dedent common leading whitespace
        lines = inner.splitlines()
        if lines:
            indents = [len(line) - len(line.lstrip()) for line in lines if line.strip()]
            if indents:
                pad = min(indents)
                lines = [line[pad:] if len(line) >= pad else line for line in lines]
        out = "\n".join(lines).strip()
        return out or None

    def is_replaceable(self, snippet: str) -> bool:
        return common_replaceable(snippet, has_doc=bool(self.extract(snippet)))
