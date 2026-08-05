from __future__ import annotations

from abc import ABC, abstractmethod

from coderazor.models import DescriptionResult


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
        system = (
            "You analyze source code across languages. "
            "Respond with valid JSON only: "
            '{"description": "40-80 word behavioral summary", "tags": ["tag1", "tag2"]}. '
            "Focus on behavior, inputs/outputs, and side effects."
        )
        user = (
            f"repo: {repo}\n"
            f"path: {path}\n"
            f"qualified_name: {qualified_name}\n"
            f"kind: {kind}\n"
            f"language: {language}\n\n"
            f"Source:\n{source_snippet}"
        )
        parsed = self.chat_json(system, user)
        return DescriptionResult(
            description=str(parsed.get("description", "")).strip(),
            tags=[str(t) for t in parsed.get("tags", [])],
        )
