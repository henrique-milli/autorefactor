# coderazor.ai architecture

## Role

coderazor is an **analysis plugin** (CLI + MCP). It produces findings, a confidence-gated **changeset queue**, and a labeled metrics report. The **host agent** applies code edits; the engine never performs structural refactors.

```text
Host agent (Cursor) ──MCP──► coderazor.mcp ──► Analysis engine
CLI (coderazor) ─────────────────────────────► Analysis engine
                                                      │
                        index → describe? → embed → analyzers → similarity
                                                      │
                                              reconcile → changesets + report
```

## Pipeline

1. **`coderazor init`** — write `.coderazor.toml` (or `~/.config/coderazor/config.toml`)
2. **`coderazor index`** — discover repos, tree-sitter extract → `out/index/`
3. **`coderazor analyze`** — describe (mode-dependent) → embed → AnalyzerPlugins + similarity/clusters → **reconcile** conflicting flags → `out/changesets/queue.json` + `out/report/` + `out/analyze/`
4. **`coderazor document`** (optional) — deterministic doc stubs for gaps
5. **`coderazor changesets`** / MCP — `list` / `next` / `mark` for the host agent loop
6. **`coderazor status` / `report`** — progress and metric highlights

### Describe modes

| Mode | Behavior |
|------|----------|
| `embed_only` | Skip LLM describe; embed normalized snippets |
| `index` | LLM describe → cache; embed description text |
| `write` | Like `index`, plus deterministic in-source stubs (Python/Java/JS/Go) |

`--skip-llm` / `skip_describe` force `embed_only`.

## Changeset queue

Stored at `out/changesets/queue.json`. `next_pending(min_confidence)` returns the highest-confidence ready item, or `{needs_confirmation: true}` when confidence is below threshold. Host marks `done` / `skipped` / `blocked` / `in_progress`.

## Metrics & visuals

`out/report/metrics.json` labels values as **exact** vs **estimate** vs **score**. Visuals (matplotlib Agg): t-SNE embedding scatter, changeset-by-action bars under `out/report/visuals/`.

## Analyzer plugins

`AnalyzerPlugin` protocol in `coderazor.analyzers`:

- **builtin** — long function, large file, duplicate name, god module
- **semgrep**, **ruff**, **eslint**, **java_static**, **coverage** — optional CLIs

Missing tools are skipped with notes in `out/analyze/analyzer-notes.json`.

## Artifact layout

| Path | Owner |
|------|--------|
| `out/index/` | index |
| `out/analyze/` | analyze findings |
| `out/changesets/` | queue for host agent |
| `out/report/` | report.md, metrics, visuals |
| `out/document/` | document stage |
| `out/cache/index.db` | description cache + sqlite snapshot |

## Providers

- `OpenAICompatibleProvider` — chat + embeddings
- `StubProvider` — offline (`--skip-llm`)

## MCP

Package `coderazor.mcp` wraps the same engine functions. Entry point: `coderazor-mcp` (FastMCP stdio).

## Extension points

- Language: `LanguageExtractor` + `extract/registry.py`
- Analyzer: implement `AnalyzerPlugin` + register in `analyzers/registry.py`
- LLM: implement `LLMProvider`
