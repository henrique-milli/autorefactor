from __future__ import annotations

import re

from autorefactor.document.packs.base import (
    AUTOREFACTOR_DOC_MARKER,
    common_replaceable,
    ensure_marker,
    strip_marker_noise,
)

_BLOCK = re.compile(r"/\*\*\s*([\s\S]*?)\s*\*/", re.M)


class JsPack:
    language = "javascript"
    aliases: tuple[str, ...] = ("typescript", "tsx", "jsx")

    def system_prompt(self, kind: str) -> str:
        depth = (
            "Use a short summary; add @param/@returns only when helpful. "
            if kind in {"function", "method"}
            else "Summarize the type/module responsibility briefly. "
        )
        return (
            "You write JSDoc for a JavaScript/TypeScript code unit. "
            "Respond with valid JSON only: "
            '{"doc": "inner JSDoc body without /** */ delimiters", "tags": ["tag1"]}. '
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
            return None
        raw = m.group(1)
        lines = [re.sub(r"^\s*\*\s?", "", line) for line in raw.splitlines()]
        inner = "\n".join(lines).strip()
        return inner or None

    def is_replaceable(self, snippet: str) -> bool:
        return common_replaceable(snippet, has_doc=bool(self.extract(snippet)))
