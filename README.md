# coderazor.ai

Analysis engine + changeset queue for host agents (CLI and MCP).

coderazor indexes polyglot workspaces, runs AnalyzerPlugins, reconciles conflicting flags into a **confidence-gated changeset queue**, and writes an honest report with metrics and visuals. The **host agent** (e.g. Cursor) applies edits using its own tokens — coderazor never applies structural refactors.

## Features

- Polyglot extraction via tree-sitter: **Java, TypeScript/TSX, Python, Go, Protobuf**
- Describe modes: `embed_only` | `index` | `write` (optional deterministic in-source doc stubs)
- Analyzer plugins: builtin smells, Semgrep, Ruff, ESLint, Java static, coverage import
- Conflict reconcile → changeset queue (`get_next` / `mark`) with confidence threshold
- Report under `out/report/` with exact / estimate / score metrics + t-SNE / bar charts
- MCP façade for Cursor (`coderazor-mcp`)

## Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Optional analyzer CLIs on PATH: `semgrep`, `eslint`, `pmd`, `coverage` / `c8`.

## Configure

**Once** via CLI:

```bash
coderazor init -w ./repos
```

Or copy [`.env.example`](.env.example) / write `.coderazor.toml`:

```toml
workspace = "/path/to/repos"
out_dir = "out"
base_url = "https://api.openai.com/v1"
api_key = "..."
llm_model = "gpt-4o-mini"
embed_model = "text-embedding-3-small"
describe_mode = "index"
confidence_threshold = 0.6
```

Offline:

```bash
coderazor analyze -w fixtures/demo-workspace -o out --skip-llm
```

## CLI

```bash
coderazor init -w ./repos
coderazor index -w ./repos -o ./out
coderazor analyze -w ./repos -o ./out
coderazor document -w ./repos -o ./out          # optional stubs
coderazor status -o ./out
coderazor report -o ./out
coderazor changesets list -o ./out
coderazor changesets next -o ./out
coderazor changesets mark <id> done -o ./out
```

### Outputs

| Path | Contents |
|------|----------|
| `out/index/` | extracted units |
| `out/analyze/` | findings, flags, analyzer notes |
| `out/changesets/queue.json` | confidence-gated queue for the host agent |
| `out/report/` | `report.md`, `metrics.json`, `visuals/` |
| `out/document/` | document gap report (optional stage) |
| `out/cache/index.db` | description cache |

## MCP (Cursor)

Add to Cursor `mcp.json`:

```json
{
  "mcpServers": {
    "coderazor": {
      "command": "coderazor-mcp",
      "args": []
    }
  }
}
```

Tools: `get_setup_requirements`, `save_config`, `analyze`, `get_report`, `list_changesets`, `get_next_changeset`, `get_changeset`, `mark_changeset`, `describe_unit`.

Host loop: `analyze` → `get_next_changeset` → apply brief → `mark_changeset`.

## Demo fixtures

```bash
coderazor analyze -w fixtures/demo-workspace -o out --skip-llm --analyzers builtin
coderazor changesets list -o out
coderazor report -o out
```

## Architecture

See [docs/architecture.md](docs/architecture.md).

## License

MIT
