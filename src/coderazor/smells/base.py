from __future__ import annotations

from typing import Protocol

from coderazor.models import CodeUnit, Flag


class SmellDetector(Protocol):
    name: str

    def detect(self, units: list[CodeUnit]) -> list[Flag]:
        ...
