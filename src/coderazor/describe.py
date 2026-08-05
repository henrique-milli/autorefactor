from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn

from coderazor.index.hierarchy import children_by_parent, rollup_description
from coderazor.index.store import IndexStore
from coderazor.models import CodeUnit, UnitKind
from coderazor.providers.base import LLMProvider

logger = logging.getLogger(__name__)

LEAF_KINDS = {
    UnitKind.FUNCTION,
    UnitKind.TYPE,
    UnitKind.PROTO_MESSAGE,
    UnitKind.PROTO_RPC,
    UnitKind.PROTO_SERVICE,
    UnitKind.FILE,
}


def describe_units(
    units: list[CodeUnit],
    provider: LLMProvider,
    store: IndexStore,
    *,
    concurrency: int = 8,
) -> list[CodeUnit]:
    by_id = {u.id: u for u in units}
    leaves = [u for u in units if u.kind in LEAF_KINDS]

    def _describe_one(unit: CodeUnit) -> CodeUnit:
        cached = store.get_description(unit.id, unit.content_hash)
        if cached:
            unit.description, unit.tags = cached
            return unit
        result = provider.describe_unit(
            repo=unit.repo,
            path=unit.path,
            qualified_name=unit.qualified_name,
            kind=unit.kind.value,
            language=unit.language,
            source_snippet=unit.source_snippet,
        )
        unit.description = result.description
        unit.tags = result.tags
        store.put_description(unit.id, unit.content_hash, result.description, result.tags)
        return unit

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
    ) as progress:
        task = progress.add_task("Describing units", total=len(leaves))
        with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
            futures = {pool.submit(_describe_one, u): u.id for u in leaves}
            for fut in as_completed(futures):
                unit = fut.result()
                by_id[unit.id] = unit
                progress.advance(task)

    # Roll up module/repo descriptions from children
    children = children_by_parent(list(by_id.values()))
    for kind in (UnitKind.MODULE, UnitKind.REPO):
        for unit in list(by_id.values()):
            if unit.kind != kind:
                continue
            kids = children.get(unit.id, [])
            unit.description = rollup_description(unit, kids)
            by_id[unit.id] = unit

    return list(by_id.values())
