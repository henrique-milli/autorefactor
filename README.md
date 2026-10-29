# autorefactor

Point your coding agent at a messy codebase. Autorefactor indexes it, finds clones and smells, and hands the agent a ranked queue of changesets—with a report UI so you can see ROI, maps, and progress.

**Beta (v0.0.1).** Upside numbers in the UI are estimates from analysis-time heuristics, not measured savings after you merge.

Site: [autorefactor.ai](https://autorefactor.ai) · License: MIT

## Who this is for

- You use Cursor (or similar) with MCP and want the agent to work a backlog, not invent one from vibes.
- You have multi-repo or polyglot trees (Java, TypeScript/TSX, Python, Go, Protobuf, SQL today).

If you need a push-button “refactor my monorepo with zero review,” this isn’t that. High-confidence items can auto-proceed; low-confidence ones stop for HITL.

## Quickstart

**Needs:** Python ≥ 3.11 (prefer [`uv`](https://docs.astral.sh/uv/)) and Node ≥ 18 for the report UI.

```bash
uv sync --extra dev

# terminal 1 — engine + HTTP API (:8765)
AUTOREFACTOR_MCP_TRANSPORT=streamable-http uv run autorefactor-mcp

# terminal 2 — report UI (:3000)
cd web && npm install && AUTOREFACTOR_API_URL=http://127.0.0.1:8765 npm run dev
```

Health: [http://localhost:3000](http://localhost:3000) (UI) and [http://localhost:8765](http://localhost:8765) (engine).

1. Open [http://localhost:3000/settings](http://localhost:3000/settings) and paste an OpenAI-compatible API key (template: **Fast cloud**).
2. Add the stdio MCP to your editor so the **project you have open** syncs to the engine (Cursor example).

Use an **absolute path** to the venv you created with `uv sync` in this repo. Cursor starts MCP without that repo as cwd, so bare `uv run autorefactor-mcp` often fails when your editor workspace is a different project.

```json
{
  "mcpServers": {
    "autorefactor": {
      "command": "/path/to/autorefactor/.venv/bin/python",
      "args": ["-m", "autorefactor.mcp.server"],
      "env": {
        "AUTOREFACTOR_REMOTE_URL": "http://127.0.0.1:8765",
        "AUTOREFACTOR_MCP_TRANSPORT": "stdio"
      }
    }
  }
}
```

Replace `/path/to/autorefactor` with your clone (e.g. `/home/you/Git/henrique/autorefactor`).

**Alternative** (only when this repo is on PATH after `uv sync`, or your MCP `cwd` is the autorefactor clone):

```json
{
  "mcpServers": {
    "autorefactor": {
      "command": "uv",
      "args": ["run", "autorefactor-mcp"],
      "env": {
        "AUTOREFACTOR_REMOTE_URL": "http://127.0.0.1:8765",
        "AUTOREFACTOR_MCP_TRANSPORT": "stdio"
      }
    }
  }
}
```

Why stdio instead of hitting `/mcp` over HTTP? The local process can see your cwd and upload uncommitted work; a bare HTTP MCP only sees the last synced workspace. Omit `AUTOREFACTOR_REMOTE_URL` entirely to run the whole pipeline in-process (no engine process needed).

3. Open **your** messy project in the editor (not this product repo unless that is what you want analyzed). [http://localhost:3000/runs](http://localhost:3000/runs) stays empty until an agent runs `analyze`.

4. In chat:

> Refactor this project with the autorefactor MCP.

The agent should analyze, show report/UI links, then ask before enrich (agent-chat mode) and before walking the queue. Inside the queue it won’t nag you on high-confidence items; it will ask on low-confidence ones.

| Service | URL |
|---------|-----|
| Report UI | http://localhost:3000 |
| Engine / health | http://localhost:8765 |

No API key → analyze fails with a setup hint. Add a key in Settings, or use local mode below.

## Local / agent-only (no cloud chat)

Host IDE does chat; Ollama does embeddings:

```bash
ollama serve
```

Settings → **Local agent** (or **Detect best setup**) → Save. For GPU embeddings the Ollama server must be able to see the GPU (`nvidia-smi` works in its environment).

## Enterprise

Default **Fast cloud** is fine for many teams: embeddings (and HTTP chat) go to whatever provider your API key points at—usually OpenAI—so **source snippets leave the machine** for those calls.

If code or prompts must stay on-prem:

1. Run a local Ollama server (`ollama serve`) instead of configuring a cloud key.
2. Settings: **Local agent** (`chat_backend=agent`, `embed_backend=local`) so chat stays in the IDE; or **Fully local chat** (`chat_backend=http` + `local_chat`, `embed_backend=local`) if you have an NVIDIA GPU with at least ~4GB VRAM.
3. Do **not** set a cloud API key (or clear it). Embeddings stay on Ollama; chat is either the IDE or a local Ollama model.

Operational constraints (same whether cloud or local):

- Ports `8765`, `3000`, and `11434` have **no auth**. Keep them on localhost / a trusted network—do not publish them.
- Keys and config live under `~/.autorefactor/config.toml`. Don’t print or commit it.
- The UI only writes Settings; it does not start the engine or Ollama.

MIT, no warranty—see [LICENSE](LICENSE).

## How it works

| Piece | Role |
|-------|------|
| Engine | Extract → embed → sensors → changeset + enrich queues |
| Host agent (MCP) | Runs analyze, optional enrich, applies edits from the queue |
| Report UI | Viewer: runs, ROI, map, queue, jobs, Settings. Does **not** start analyze |

Artifacts default to `~/.autorefactor`. Local projects are synced there (`~/.autorefactor/workspaces/active`, tar.gz over HTTP) before analysis.

More on axes and backends: [docs/runtime-backends.md](docs/runtime-backends.md).

## Configuration

Axes (Settings or `AUTOREFACTOR_*` env):

| Axis | Values | Notes |
|------|--------|-------|
| `chat_backend` | `agent` \| `http` \| `none` | IDE vs OpenAI-compatible HTTP |
| `embed_backend` | `local` \| `http` \| `none` | Ollama vs cloud |
| `embed_device` | `cpu` \| `gpu` | Local embed model pick |
| `describe_mode` | `raw` \| `describe` | Embed source vs summarize-then-embed |
| `local_chat` | bool | HTTP chat routed to Ollama |

Unset axes default to cloud `http` / `http` (key required).

`.env` is optional. [`.env.example`](.env.example) lists infra knobs (ports, workspace path, models). Every Settings field has a matching env var.

## Development

```bash
uv sync --extra dev
uv run pytest
```

Run engine + UI as in the Quickstart.

## Status / limits

- Beta: APIs and queue scoring will move.
- Semgrep / ESLint / Java static sensors need those CLIs on the host when you enable the full profile.
- Issues and PRs welcome once the repo is public; no CONTRIBUTING guide yet—keep changes small and include tests.

## License

[MIT](LICENSE)
