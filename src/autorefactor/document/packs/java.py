from __future__ import annotations

import re

from autorefactor.document.packs.base import (
    AUTOREFACTOR_DOC_MARKER,
    common_replaceable,
    ensure_marker,
    strip_marker_noise,
)

_BLOCK = re.compile(r"/\*\*\s*([\s\S]*?)\s*\*/", re.M)


class JavaPack:
    language = "java"
    aliases: tuple[str, ...] = ()

    def system_prompt(self, kind: str) -> str:
        depth = (
            "Use a short summary plus @param/@return when the unit is non-trivial. "
            if kind in {"function", "method"}
            else "Summarize the type's responsibility in 1-3 sentences. "
        )
        return (
            "You write Java documentation for a code unit. "
            "Respond with valid JSON only: "
            '{"doc": "inner Javadoc body without /** */ delimiters", "tags": ["tag1"]}. '
            f"{depth}"
            "Focus on behavior, inputs/outputs, and side effects. "
            f"Include the token {AUTOREFACTOR_DOC_MARKER} once in the first line of doc. "
            "Do not wrap doc in comment delimiters."
        )

    def wrap(self, inner: str) -> str:
        body = ensure_marker(strip_marker_noise(inner))
        lines = body.splitlines() or [body]
        if len(lines) == 1:
            return f"{{PREFIX}}/** {lines[0]} */\n"
        mid = "\n".join(f"{{PREFIX}} * {line}" if line.strip() else "{PREFIX} *" for line in lines)
        return f"{{PREFIX}}/**\n{mid}\n{{PREFIX}} */\n"

    def extract(self, snippet: str) -> str | None:
        text = snippet or ""
        m = _BLOCK.search(text[:2000])
        if not m:
            stripped = text.lstrip()
            if stripped.startswith("/**"):
                m = _BLOCK.search(stripped[:2000])
        if not m:
            return None
        raw = m.group(1)
        lines = []
        for line in raw.splitlines():
            lines.append(re.sub(r"^\s*\*\s?", "", line))
        inner = "\n".join(lines).strip()
        return inner or None

    def is_replaceable(self, snippet: str) -> bool:
        return common_replaceable(snippet, has_doc=bool(self.extract(snippet)))
