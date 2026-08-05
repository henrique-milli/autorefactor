# coderazor agent-plugin redesign

**Date:** 2026-08-05  
**Status:** Approved in brainstorming (sections §1–§5)  
**Approach:** B — analysis engine + changeset store + CLI + MCP façade

## Problem

Auto-refactor (OpenHands apply) promises autonomy we do not have while HITL is required. A second agent stack also fights subsidized host-agent tokens. The valuable product is: analyze a (multi-)repo workspace with OSS tools + embeddings, reconcile conflicting flags with an LLM, and deliver an iterable queue of isolated changesets plus a motivating, honest report — executed by the host agent.

## Goals

- Deterministic analysis engine (index, optional describe, embed, OSS flags, similarity, clusters).
- LLM describe of units with configurable precision; optional write of Javadoc/JSDoc/pydoc/godoc into source.
- LLM reconcile of conflicting flags into ranked decisions / changesets.
- Host-agent loop via MCP: `get_next_changeset` → apply → `mark_changeset` (no OpenHands apply).
- Human report with measurable and clearly labeled estimable metrics + embedding/before-after visuals.
- Fully configurable providers (OpenAI-compatible base URL/key/model, including local).
- Guided setup once (CLI `init` + MCP setup tools), not a quiz on every invoke.
- Multi-repo data model from day one; single-repo is N=1.

## Non-goals (v1)

- OpenHands / autonomous apply agent.
- Cloud PR bot that responds to review comments.
- SonarQube integration.
- Interactive setup prompts on every `analyze` tool call.
- Presenting soft heuristics as measured bug-reduction percentages without labeling.

## Architecture

```text
Host agent (Cursor) — subsidized tokens, applies code edits
        ↕ MCP tools
MCP façade  |  CLI (init / analyze / report / changesets / status)
        ↕
Analysis engine (non-interactive)
  1. Index (tree-sitter, multi-repo workspace)
  2. Describe (optional) → index cache and/or write docs
  3. Embed (OpenAI-compatible or local)
  4. Flag (OSS analyzers + similarity + clusters)
  5. Reconcile (LLM: conflicts → decisions)
  6. Report + visuals + changeset queue on disk
```

Hard rules:

- Engine never applies structural refactors.
- Corpus stays on disk; agent context is O(1) changeset briefs.
- Config is persistent; setup runs only when incomplete.

## Describe / embed modes

| Mode | Behavior |
|------|----------|
| `embed_only` | Normalized code representation → embeddings (no chat describe pass). |
| `index` | LLM behavioral summaries cached in the index only (recommended precision default when a cheap model like luna is configured). |
| `write` | Same as `index`, plus optional in-source docs (Javadoc/JSDoc/pydoc/godoc) with explicit consent via config. |

Embeddings use a consistent representation (descriptions when present, else normalized code). Providers: any OpenAI-compatible chat + embed endpoints.

## Flagging and reconcile

- **OSS analyzers:** builtin smells, Semgrep, Ruff, ESLint, Java static (PMD/SpotBugs), coverage import when available; missing tools skip with notes.
- **Similarity:** near-duplicate / merge candidates among leaf units.
- **Clusters:** colocation / “same module” suggestions.
- **Reconcile:** LLM collapses overlapping/conflicting flags (e.g. move vs merge on the same method) into non-overlapping decisions with confidence scores. Failures are scoped per conflict component (`unresolved`), not whole-run abort.

## Changeset model

Each changeset includes:

- `id`, `title`, `action`
- `brief` — enough for one host-agent coding session
- `rationale`, `confidence` (0–1)
- `targets[]` (repo / path / symbol)
- `depends_on[]`, `parallel_group`
- `acceptance` (verification hints)
- `metrics_delta` (estimated impact for this change)
- `status`: `pending` | `in_progress` | `done` | `skipped` | `blocked`
- `evidence_refs` (flag/unit ids; not full source dumps)

Agent loop:

1. `get_next_changeset` — if confidence < configured threshold → `needs_confirmation` + questions for the user.
2. Host agent edits (or asks user).
3. `mark_changeset`.
4. Repeat until queue empty.

Independent `parallel_group` items with empty `depends_on` may run as parallel isolated sessions; the on-disk store is source of truth.

## CLI

- `coderazor init` — guided setup wizard.
- `coderazor analyze` — full engine pipeline through report + queue.
- `coderazor status` / `report` — progress and report paths.
- `coderazor changesets list|next|mark` — queue ops without MCP.
- Remove first-class `refactor` / OpenHands apply commands.

## MCP tools

Firebase-style: wrap the CLI/engine with schemas and short usage guidance.

| Tool | Purpose |
|------|---------|
| `get_setup_requirements` / `save_config` | Agent-led first-run setup |
| `analyze` | Run/resume analysis |
| `get_report` | Metric highlights + paths to report/visuals |
| `list_changesets` | Compact queue listing |
| `get_next_changeset` | One brief; may need user confirmation |
| `get_changeset` | By id |
| `mark_changeset` | Status update |
| `describe_unit` | Optional on-demand describe |

Large payloads are file paths under `out/`, not inline blobs.

## Report, metrics, visuals

Artifacts under `out/report/`: `report.md`, `report.json`, `metrics.json`, `visuals/`.

| Metric | Treatment |
|--------|-----------|
| Current LOC / files / units | Exact from index |
| Expected LOC reduction | Estimate from removable spans × confidence; labeled estimate |
| Expected token reduction % | Estimate from source/embed-text proxy; labeled estimate |
| Tech-debt score | Heuristic from smell severity; labeled score |
| Bug reduction | Prefer “risk surface reduced (N duplicate paths)” over undefended bug % |

Visuals (engine-generated, offline):

1. 2D projection of embeddings (UMAP/t-SNE), colored by repo/cluster, changeset targets highlighted.
2. Before/after schematic of affected modules/files for pending vs projected structure.

## Config

Persistent `.coderazor.toml` and/or env: workspace, out dir, exclude repos, chat/embed base URL + key + model, `describe_mode`, analyzers, confidence threshold, concurrency. Local models supported via base URL.

## Scale and errors

- Batched describe/embed with content-hash cache; configurable concurrency.
- Similarity on leaf units only; reconcile by conflict connected-component.
- Analyzer missing → skip + note. Provider errors → retry/backoff; resumable caches.
- MCP/CLI return `needs_setup` when config incomplete.

## Testing

- Unit: conflict graph, changeset IO, metrics estimators, config.
- Contract: analyze produces queue + report; MCP schemas call engine stubs.
- Fixtures: existing demo workspace for offline paths.

## Migration from current codebase

- Keep: extractors, analyzers, index/cache, embed, similarity/clusters, staged `out/` layout ideas.
- Repurpose: plan → changeset queue; confirm → reconcile.
- Delete/demote: OpenHands apply/`refactor`/`run` ending in autonomous edits.
- Add: MCP server package, `init`/setup tools, report metrics + visuals, confidence-gated `get_next`.

## Success criteria

- User can `init`, `analyze`, open a report with labeled metrics and at least one embedding visual.
- MCP agent can drain the queue via `get_next` / `mark` without loading all flags into context.
- No OpenHands dependency required for the default path.
- Multi-repo workspace works; single-repo works unchanged.
