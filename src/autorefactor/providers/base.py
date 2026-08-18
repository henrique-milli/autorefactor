from __future__ import annotations

from abc import ABC, abstractmethod

from autorefactor.document.packs import get_pack
from autorefactor.document.packs.base import ensure_marker
from autorefactor.models import DescriptionResult


_GENERIC_SYSTEM = (
    "You analyze source code across languages. "
    "Respond with valid JSON only: "
    '{"doc": "40-80 word behavioral summary", "tags": ["tag1", "tag2"]}. '
    "Focus on behavior, inputs/outputs, and side effects. "
    'The legacy key "description" is also accepted as an alias for doc.'
)


class LLMProvider(ABC):
    @abstractmethod
    def chat_json(self, system: str, user: str, *, temperature: float = 0.2) -> dict:
        raise NotImplementedError

    @abstractmethod
    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError

    def describe_unit(
        self,
        *,
        repo: str,
        path: str,
        qualified_name: str,
        kind: str,
        language: str,
        source_snippet: str,
    ) -> DescriptionResult:
        pack = get_pack(language)
        system = pack.system_prompt(kind) if pack is not None else _GENERIC_SYSTEM
        user = (
            f"repo: {repo}\n"
            f"path: {path}\n"
            f"qualified_name: {qualified_name}\n"
            f"kind: {kind}\n"
            f"language: {language}\n\n"
            f"Source:\n{source_snippet}"
        )
        parsed = self.chat_json(system, user)
        raw = str(parsed.get("doc") or parsed.get("description") or "").strip()
        if pack is not None and raw:
            # Model may accidentally return a full comment block; unwrap if so.
            if raw.lstrip().startswith(("/**", '"""', "'''", "//")):
                extracted = pack.extract(raw if raw.lstrip().startswith("//") else raw)
                if not extracted and raw.lstrip().startswith("/**"):
                    extracted = pack.extract(raw + "\nvoid __probe() {}")
                if extracted:
                    raw = extracted
            raw = ensure_marker(raw)
        return DescriptionResult(
            description=raw,
            tags=[str(t) for t in parsed.get("tags", [])],
        )
