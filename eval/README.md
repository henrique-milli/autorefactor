# Agent-teams eval (local)

**Self-improve loop first** (users → evidence → patch → spec-probe retest on every episode): **[docs/self-improve.md](../docs/self-improve.md)**.

```bash
uv sync --extra eval --extra dev
printf 'CURSOR_API_KEY=cursor_...\n' > eval/secrets.local.env   # gitignored
uv run ar-eval loop --iterations 3 --example-only
```

Useful flags: `--strict`, `--skip-retest`, `--retest-agent`, `--alpha-timeout 300`, `--rotate-pairs`, `--max-alpha-changes 3`, `--analyze-skip-llm`. Green retest is **all-episode spec probes** (`2/2 pass`), not a lone probe score.

## Subject wave (legacy: critic → maintainer on stimulus trees)

```bash
docker compose up -d --build
uv run ar-eval wave --skip-agents --example-only --max-episodes 1
uv run ar-eval wave --example-only --max-episodes 1
```

Legacy (no subcommand) still runs a wave with the same flags. Prefer `loop` for product self-improve.

## Corpus

- Committed: [`corpus.example.yaml`](corpus.example.yaml) (fixtures: `demo-alpha`, `slop-twins`).
- Optional gitignored overlay: `corpus.local.yaml` (absolute paths to any local checkouts). Local `episodes` replace the example when present; use `--example-only` to force fixtures.

## Engine refresh

Waves sync via HTTP. `--restart-engine` on `wave` restarts the container without rebuild. The **loop** always restarts between **episode** syncs, holds `eval/artifacts/.engine.lock` during sync/analyze/rebuild, and runs `docker compose restart autorefactor` **after smoke is green and alpha applied** (unless `--skip-rebuild`) so bind-mounted `src/` is re-imported. Image `--build` happens at loop start (`ensure_engine`) and when Dockerfile/deps change. Critics sample the queue in parallel and do not apply. Red smoke is sent back to alpha to fix; rebuild/retest wait.

## Safety

- Do not commit `corpus.local.yaml`, `secrets.local.env`, `subjects/`, or `artifacts/`.
- Local agents only; no PRs from this harness.
- Loop agents always **copy** subjects (never symlink). Critics are read-mostly and must not edit the copy.
