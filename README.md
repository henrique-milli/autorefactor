# autorefactor

Point your coding agent at a messy codebase. Autorefactor indexes it, finds clones and smells, and hands the agent a ranked queue of changesets—with a report UI so you can see ROI, maps, and progress.

**Beta (v0.0.1).** Upside numbers in the UI are estimates from analysis-time heuristics, not measured savings after you merge.

Site: [autorefactor.ai](https://autorefactor.ai) · License: MIT

## Who this is for

- You use Cursor (or similar) with MCP and want the agent to work a backlog, not invent one from vibes.
- You have multi-repo or polyglot trees (Java, TypeScript/TSX, Python, Go, Protobuf today).
- You’re fine running Docker locally (or Podman / Rancher Desktop with the same Compose files).

If you need a push-button “refactor my monorepo with zero review,” this isn’t that. High-confidence items can auto-proceed; low-confidence ones stop for HITL.

## Quickstart

**Needs:** Docker Compose, Python ≥ 3.11 on the host (for the MCP stdio proxy). Prefer `uv`.

```bash
uv sync --extra dev
docker compose up -d --build
```

Health: [http://localhost:3000](http://localhost:3000) (UI) and [http://localhost:8765](http://localhost:8765) (engine).

1. Open [http://localhost:3000/settings](http://localhost:3000/settings) and paste an OpenAI-compatible API key (template: **Fast cloud**).
2. Add the stdio proxy to your editor so the **project you have open** syncs to the engine (Cursor example):

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

`uv run autorefactor-mcp` is the primary path. After `uv sync`, `autorefactor-mcp` on PATH also works. Why `command` instead of hitting `/mcp` in the browser? The local process can see your cwd and upload uncommitted work; a bare HTTP MCP only sees the container workspace.

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
docker compose --profile ollama up -d --build
```

Settings → **Local agent** (or **Detect best setup**) → Save.

NVIDIA GPU for Ollama:

```bash
docker compose -f docker-compose.yml -f docker-compose.gpu.yml --profile ollama up -d --build
```

## Enterprise

Default **Fast cloud** is fine for many teams: embeddings (and HTTP chat) go to whatever provider your API key points at—usually OpenAI—so **source snippets leave the machine** for those calls.

If code or prompts must stay on-prem:

1. Start Compose with `--profile ollama` (GPU overlay if you have NVIDIA—see above).
2. Settings: **Local agent** (`chat_backend=agent`, `embed_backend=local`) so chat stays in the IDE; or **Fully local chat** (`chat_backend=http` + `local_chat`, `embed_backend=local`) if you have an NVIDIA GPU with at least ~4GB VRAM.
3. Do **not** set a cloud API key (or clear it). Embeddings stay on Ollama; chat is either the IDE or a local Ollama model.

Operational constraints (same whether cloud or local):

- Ports `8765`, `3000`, and `11434` have **no auth**. Keep them on localhost / a trusted network—do not publish them.
- Keys and config live under `./.autorefactor-data` (container `~/.autorefactor`). Don’t commit that directory.
- The UI does not control Docker (no socket mount, no DinD).
- Podman / Rancher Desktop: same Compose files; use your compose CLI if you don’t use Docker Engine.

MIT, no warranty—see [LICENSE](LICENSE).

## How it works

| Piece | Role |
|-------|------|
| Engine (Compose) | Extract → embed → sensors → changeset + enrich queues |
| Host agent (MCP) | Runs analyze, optional enrich, applies edits from the queue |
| Report UI | Viewer: runs, ROI, map, queue, jobs, Settings. Does **not** start analyze |

Artifacts default to `./.autorefactor-data`. Workspace defaults to the Compose mount (`.` → `/workspace`).

More on axes and compose profiles: [docs/runtime-backends.md](docs/runtime-backends.md).

## Self-improve loop

Developers of this product can run simulated users against stimulus repos, patch allowlisted product code, and retest. **Green retest means spec probes on every episode**, not a dashboard `1.00` from the first repo. Details and flags (`--alpha-timeout`, `--retest-agent`, `--skip-retest`): [docs/self-improve.md](docs/self-improve.md). `uv sync --extra eval && uv run ar-eval loop --iterations 3 --example-only`.

## Configuration

Axes (Settings or `AUTOREFACTOR_*` env):

| Axis | Values | Notes |
|------|--------|--------|
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

Engine without the web image: `AUTOREFACTOR_MCP_TRANSPORT=streamable-http uv run autorefactor-mcp`, and `cd web && npm run dev` with `AUTOREFACTOR_API_URL=http://127.0.0.1:8765`.

## Status / limits

- Beta: APIs and queue scoring will move.
- Semgrep / ESLint / Java static sensors need those CLIs on the engine image or host when you enable the full profile.
- Issues and PRs welcome once the repo is public; no CONTRIBUTING guide yet—keep changes small and include tests.

## License

[MIT](LICENSE)
