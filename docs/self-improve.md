# Self-improve loop

Unattended local loop: **simulated users sample the product → hard-evidenced feedback → patch product → retest**.

Default shape is **cartesian**: every iteration walks **all user personas × all corpus episodes**. Personas run **in parallel** within an episode; episodes run **sequentially** (engine lock around sync/analyze/rebuild only). `--rotate-pairs` is a cheap debug mode (1 persona × 1 episode). Do not shrink the cartesian walk to fake progress.

**Green retest means spec probes passed on every episode**, not a lone `1.00` from the first repo or an optional Cursor scorer. Leftover sampled changeset IDs are **notes** (`sticky_ids`); they do not fail the run. Later critic walks see the last episode’s probe counts (`ensure_tests_count`) so they cannot re-file a miss the harness already measured closed.

Eval probes check **spec acceptance** (prep exists, secrets stay HITL) — not sticky changeset ids and not “every `ensure_tests` is HITL.”

## Critics are OSS-user simulations

User personas clone this product, point MCP at **their** subject tree, and judge whether the queue is usable. The harness copies `docs/product-goal.md`, a README quickstart excerpt, and a tiny `product_index.md` into `.autorefactor-context/` (ignored by the snapshot/evidence gate). Critics **Read 1–2 cited subject files** and compare briefs vs code. They must not write subject or product files. Coverage-purist judges prep quality/ordering; confidence-skeptic judges **wrong** auto_proceed — not high-conf splits and not “never autorun.”

**Triage may not ship always-HITL.** Accept goal-aligned patches (usable autorun, honest report, prep order, real slop/secret HITL). Reject extra confirm gates, confidence caps as judgment, playbook essays, and subject bugs without a queue/report miss.

## Cycle (honest)

1. **Ensure engine**: `docker compose up -d --build autorefactor` + health (no manual compose needed).
2. **Copy** stimulus repo into `eval/subjects/`.
3. Snapshot file hashes; **restart + sync + analyze** under an engine file-lock (**once per episode**).
4. **All personas in parallel** (read-mostly sample walk): `get_status` / `get_report` / `sample_queue` (1 autorun brief + 1–3 HITL briefs). They do **not** confirm, mark, or edit subject files. Later iterations inject that persona+episode’s prior findings plus the last `alpha_spec` so critics mark `still_open` / `resolved` / `new`.
5. **Hard evidence gate**: `source=product_use`, report reviewed, queue sample (`sample_queue` or `list_changesets`+`get_changeset`), no queue mutation, no subject diffs. Alpha proceeds if **at least one** walk passes. Failed gate skips triage, alpha, and rebuild.
6. **Triage** merges tagged findings (`episode:persona:local_id`) → ≤3 ranked `alpha_spec` changes.
7. **Alpha improver** patches allowlisted product paths including `tests/**` (fail-soft `--alpha-timeout`, default 300s; smoke still runs on whatever landed). Empty spec skips alpha and rebuild.
8. **Smoke** (pytest). If it fails: send the traceback to alpha for a repair pass, then smoke again. **Do not rebuild or retest** while smoke is red. `--strict` aborts only after that repair still fails.
9. Smoke green and alpha applied → `docker compose restart autorefactor` (bind-mounted `src/` re-imported; web stays up). Image `--build` is only at loop start or when deps/Dockerfile change. `--skip-rebuild` skips the restart; retest still runs if alpha applied.
10. **Retest** (only if alpha applied): per-episode re-analyze + probes derived from **this iteration’s sanitized `alpha_spec`**. `passed` requires **all** episodes ok. Sticky reorder if any episode fails. Dashboard shows `retest 2/2 pass` (or `1/2 fail`). Opt-in `--retest-agent` ANDs a Cursor scorer; it is never the sole pass bit. `--skip-retest` skips analyze+probe entirely.

Failed retest **reorders** that episode first next iteration (does not drop the rest of the corpus).

## Setup

```bash
printf 'CURSOR_API_KEY=cursor_...\n' > eval/secrets.local.env
uv sync --extra eval --extra dev
uv run ar-eval loop --iterations 3
```

The loop **ensures** Compose at start (`docker compose up -d --build autorefactor` + health), **restarts** the engine between stimulus **episodes**, and **restarts the engine again** after each alpha patch so bind-mounted `src/` is re-imported (web stays up). You do not need a manual `docker compose up` first (unless you pass `--no-engine`).

Run `ar-eval` from this product repo. Stdio MCP for user/retest agents is pinned to the materialized copy under `eval/subjects/` (`cwd` + `AUTOREFACTOR_MCP_WORKSPACE`). The product checkout is never auto-synced over an episode analyze.

Optional gitignored `eval/corpus.local.yaml` for private absolute roots.

## Run

```bash
uv run ar-eval loop --iterations 3 --example-only
uv run ar-eval loop --iterations 2 --episode demo-alpha
uv run ar-eval loop --rotate-pairs --iterations 4
```

| Flag | Meaning |
|------|---------|
| `--iterations N` | Outer loop count |
| `--example-only` | Ignore local corpus |
| `--episode ID` | Restrict episodes (example corpus: `demo-alpha`, `slop-twins`) |
| `--rotate-pairs` | One persona × one episode per iter (debug) |
| `--all-episodes` | Full corpus (default; compatibility flag) |
| `--strict` | Abort after smoke repair still fails, or **any** episode spec-probe retest fails |
| `--skip-rebuild` | Skip compose restart after alpha (dev). Retest still runs if alpha applied. |
| `--skip-retest` | Skip analyze+probe retest (dev). Default retest is probe-only (no Cursor agent). |
| `--retest-agent` | After probe, also run the opt-in Cursor scorer on prior finding ids (AND with probe) |
| `--max-alpha-changes N` | Triage budget (default 3) |
| `--alpha-timeout S` | Fail-soft seconds for alpha / smoke-repair agents (default 300). Timeout is not a `--strict` failure if smoke is green. |
| `--analyze-skip-llm` | Faster analyze |
| `--no-analyze` | Only with `--skip-agents` |
| `--no-progress` | Timestamped lines instead of a live dashboard |
| `--json` | Also print the full summary JSON after the short table |

## Artifacts

```
eval/artifacts/loop/iter-001/<episode>/
  feedback.json                 # episode merge
  <persona-stem>/feedback.json  # one walk
  <persona-stem>/prior_feedback.json
  retest_probe.json retest_score.json
eval/artifacts/loop/iter-001/feedback_merged.json
eval/artifacts/loop/iter-001/timing.json
eval/artifacts/loop/iter-001/retest_summary.json
```

`timing.json` records **every** episode’s retest. `agents.critics_wall_s` is the **sum** of sequential episode critic walls (each episode wall is the max of parallel personas).

## Safety

Local Cursor only; disposable subject copies; no auto-commit/PRs. Product edits limited to `src/autorefactor/**`, `eval/personas|schemas/**`, `docs/**`, `tests/**`. Shared engine access is serialized via `eval/artifacts/.engine.lock` during sync/analyze/rebuild only — persona sample walks are read-only and parallel.

Do not commit `eval/artifacts/`, `eval/corpus.local.yaml`, `eval/secrets.local.env`, or `.env`.
