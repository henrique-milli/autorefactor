from __future__ import annotations

from coderazor.models import Flag


def merge_flags(*flag_groups: list[Flag]) -> list[Flag]:
    by_id: dict[str, Flag] = {}
    for group in flag_groups:
        for flag in group:
            existing = by_id.get(flag.id)
            if existing is None or flag.score > existing.score:
                by_id[flag.id] = flag
    return sorted(by_id.values(), key=lambda f: (-f.score, f.title))
