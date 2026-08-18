from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn

from autorefactor.document.packs import get_pack
from autorefactor.index.hierarchy import children_by_parent, rollup_description
from autorefactor.index.store import IndexStore
from autorefactor.domain import CodeUnit, UnitKind
from autorefactor.providers.base import LLMProvider

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
    stats: dict[str, int] | None = None,
) -> list[CodeUnit]:
    by_id = {u.id: u for u in units}
    leaves = [u for u in units if u.kind in LEAF_KINDS]
    stats_out = stats if stats is not None else {}
    stats_out.setdefault("describe_cache_hits", 0)
    stats_out.setdefault("describe_cache_misses", 0)
    stats_out.setdefault("describe_source_seed_hits", 0)
    stats_out.setdefault("describe_llm_calls", 0)
    stats_lock = threading.Lock()

    def _describe_one(unit: CodeUnit) -> CodeUnit:
        cached = store.get_description(unit.id, unit.content_hash)
        if cached:
            with stats_lock:
                stats_out["describe_cache_hits"] += 1
            unit.description, unit.tags = cached
            return unit
        with stats_lock:
            stats_out["describe_cache_misses"] += 1
        pack = get_pack(unit.language)
        if pack is not None:
            seeded = pack.extract(unit.source_snippet or "")
            if seeded:
                with stats_lock:
                    stats_out["describe_source_seed_hits"] += 1
                unit.description = seeded
                unit.tags = ["from_source"]
                store.put_description(unit.id, unit.content_hash, seeded, unit.tags)
                return unit
        with stats_lock:
            stats_out["describe_llm_calls"] += 1
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

    # Roll up type descriptions from child methods, then module/repo
    children = children_by_parent(list(by_id.values()))
    for kind in (UnitKind.TYPE, UnitKind.PROTO_SERVICE, UnitKind.MODULE, UnitKind.REPO):
        for unit in list(by_id.values()):
            if unit.kind != kind:
                continue
            kids = children.get(unit.id, [])
            # Prefer function children for type rollup
            if kind == UnitKind.TYPE:
                preferred = [k for k in kids if k.kind == UnitKind.FUNCTION and k.description]
                if preferred:
                    kids = preferred
            # Always refresh type rollup when children have descriptions
            if kids and any(k.description for k in kids):
                unit.description = rollup_description(unit, kids)
                by_id[unit.id] = unit
            elif unit.kind in {UnitKind.MODULE, UnitKind.REPO}:
                unit.description = rollup_description(unit, kids)
                by_id[unit.id] = unit

    return list(by_id.values())
