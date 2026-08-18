from __future__ import annotations

from autorefactor.document.packs.base import AUTOREFACTOR_DOC_MARKER, DocPack, ensure_marker
from autorefactor.document.packs.go import GoPack
from autorefactor.document.packs.java import JavaPack
from autorefactor.document.packs.js import JsPack
from autorefactor.document.packs.python import PythonPack

_PACKS: list[DocPack] = [JavaPack(), PythonPack(), JsPack(), GoPack()]
_BY_LANG: dict[str, DocPack] = {}
for _p in _PACKS:
    _BY_LANG[_p.language.lower()] = _p
    for alias in getattr(_p, "aliases", ()):
        _BY_LANG[str(alias).lower()] = _p


def get_pack(language: str) -> DocPack | None:
    return _BY_LANG.get((language or "").strip().lower())


__all__ = [
    "AUTOREFACTOR_DOC_MARKER",
    "DocPack",
    "ensure_marker",
    "get_pack",
]
