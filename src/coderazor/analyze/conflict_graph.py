from __future__ import annotations

from coderazor.models import Flag


def conflict_components(flags: list[Flag]) -> list[list[Flag]]:
    """Group flags that share unit_ids into connected components."""
    if not flags:
        return []
    unit_to_flags: dict[str, list[int]] = {}
    for idx, flag in enumerate(flags):
        for uid in flag.unit_ids:
            unit_to_flags.setdefault(uid, []).append(idx)

    parent = list(range(len(flags)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for indices in unit_to_flags.values():
        for i in range(1, len(indices)):
            union(indices[0], indices[i])

    buckets: dict[int, list[Flag]] = {}
    for idx, flag in enumerate(flags):
        buckets.setdefault(find(idx), []).append(flag)
    return list(buckets.values())
