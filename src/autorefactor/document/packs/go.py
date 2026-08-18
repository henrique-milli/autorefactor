from __future__ import annotations

import re

from autorefactor.document.packs.base import (
    AUTOREFACTOR_DOC_MARKER,
    common_replaceable,
    ensure_marker,
    strip_marker_noise,
)

_LINE_RUN = re.compile(r"(?:^[ \t]*//[^\n]*\n)+", re.M)


class GoPack:
    language = "go"
    aliases: tuple[str, ...] = ()

    def system_prompt(self, kind: str) -> str:
        return (
            "You write Go doc comments for a code unit. "
            "Respond with valid JSON only: "
            '{"doc": "inner doc comment text without // prefixes", "tags": ["tag1"]}. '
            "Start with the symbol name when documenting an exported identifier. "
            "Keep comments concise. "
            f"Include the token {AUTOREFACTOR_DOC_MARKER} once in the first line of doc. "
            "Do not prefix lines with //."
        )

    def wrap(self, inner: str) -> str:
        body = ensure_marker(strip_marker_noise(inner))
        lines = body.splitlines() or [body]
        return "".join(f"{{PREFIX}}// {line}\n" if line.strip() else "{PREFIX}//\n" for line in lines)

    def extract(self, snippet: str) -> str | None:
        text = snippet or ""
        # Leading // comments before func/type
        m = re.match(r"(?:[ \t]*//[^\n]*\n)+", text.lstrip())
        if not m:
            # Search near top of snippet
            m = _LINE_RUN.search(text[:1500])
            if not m or m.start() > 80:
                return None
        block = m.group(0)
        lines = []
        for line in block.splitlines():
            lines.append(re.sub(r"^[ \t]*//\s?", "", line))
        inner = "\n".join(lines).strip()
        return inner or None

    def is_replaceable(self, snippet: str) -> bool:
        return common_replaceable(snippet, has_doc=bool(self.extract(snippet)))
