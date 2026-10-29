# Runtime backends

Axes (Settings or `AUTOREFACTOR_*` env):

| Axis | Values | Role |
|------|--------|------|
| `chat_backend` | `agent` \| `http` \| `none` | Host IDE vs OpenAI-compatible HTTP vs off |
| `embed_backend` | `local` \| `http` \| `none` | Ollama vs cloud embeddings vs off |
| `embed_device` | `cpu` \| `gpu` | Local embed model selection |
| `describe_mode` | `raw` \| `describe` | Embed source vs summarize-then-embed |
| `local_chat` | bool | When `chat_backend=http`, route chat to Ollama |

**Default (axes unset):** `http` / `http` (cloud). Missing key → setup error pointing at Settings or local mode.

## Models (2026)

| Role | Model |
|------|-------|
| Local chat | `gemma4:e2b` |
| Local embed (CPU) | `nomic-embed-text` |
| Local embed (GPU) | `qwen3-embedding:0.6b` |
| Cloud chat | `gpt-6-luna` |
| Cloud embed | `text-embedding-3-small` |

## Running

| Path | Command |
|------|---------|
| Engine + API + MCP | `AUTOREFACTOR_MCP_TRANSPORT=streamable-http uv run autorefactor-mcp` |
| Report UI | `cd web && AUTOREFACTOR_API_URL=http://127.0.0.1:8765 npm run dev` |
| Local embeddings | `ollama serve` (CPU or GPU, if the server can see it) |

Settings edits axes only. `GET /v1/runtime/detect` recommends axes (GPU/Ollama best-effort).

## Polyglot contract workflow

Multi-language repos (Python + Go + TypeScript + proto, etc.) should treat **proto/spec as source of truth** and drive implementations from it:

1. **Index + analyze** — run `autorefactor analyze` (or MCP `analyze`) with `test_quality` and `contract` sensors enabled (included in core sensors).
2. **Read contract findings** — the `contract` sensor clusters cross-language units by normalized names (`snake_case` ↔ `camelCase` ↔ `PascalCase` in finding `metadata.naming_map`) and flags surface drift (e.g. `merge_scores` in Python+proto but missing in TypeScript) or missing golden/contract tests.
3. **Read test_quality findings** — flags shallow `(autorefactor ensure-tests)` stubs that only assert `symbol_exists` / `is_importable` or duplicate per-file import boilerplate. Treat these as **not covered** even when test files exist.
4. **ensure_tests prep (MCP/CLI)** — before accepting scaffolds, resolve contract drift and replace symbol-exists stubs with **behavioral** tests: parametrized input→expected vectors (see `normalize_user_id` edge cases) and one module per TYPE (e.g. `ScoreAggregator` add+snapshot together). Scaffolding writes shared `tests/conftest.py` instead of repeating `sys.path` hacks; prefer `pip install -e .` when possible.
5. **Golden vectors** — add `tests/contract/` or proto-driven conformance tests that share vectors across languages; link RPC names from `.proto` to each language export via the naming map.

When planning refactors on polyglot clusters, fix contract parity first, then expand behavioral coverage — not import-only pytest files.

## JVM / Spring services

Spring/Kafka microservices benefit from reading **`wiring`**, **`test_quality`**, **`sim.method`** (similarity), and **`structure`** findings together before queueing refactors:

| Finding kind | Sensor | Queue priority guidance |
|--------------|--------|-------------------------|
| Orphan Kafka / test-only `@KafkaListener` handlers | `wiring` | **Wire the consumer first** — handlers exercised only from `src/test` are not production-ready; do not refactor merge/DTO paths until messages flow. |
| Asymmetric ingestion dedupe | `wiring` | Align idempotency (`findBy*` vs always-increment) before changing aggregation logic. |
| Mock-only Java suites (≥8 test classes, no `@SpringBootTest`) | `test_quality` | **Add `@SpringBootTest` / Testcontainers** before DTO or mapper refactors — WebMvcTest/Mockito smoke hides wiring bugs. |
| Coverage-padding `*CoverageTest` | `test_quality` | Replace getter/setter Jacoco padding with integration assertions (HTTP/Mongo/Kafka). |
| Divergent merge paths (`mapTo*Response` / `merge*`) | `sim.method` | Unify append-vs-sum merge APIs **after** consumer wiring; see `structure` for dead test-only APIs. |
| Committed secrets / README build drift | `structure` | Rotate credentials and fix docs before agents follow stale `mvn` vs `gradle` instructions. |
| Draft in-memory maps / disabled startup validation | `slop` | Resolve config validation and mapping sources before feature work. |

Recommended analyze prep: `autorefactor analyze` with core sensors. Prioritize: **wire consumer → integration tests → unify merge API → layer cleanup**.

## Status

- `GET /` — HTML status
- `GET /health` — backends / `ollama_required` / `ollama_ok`
- `GET /v1/runtime/detect` — nvidia + ollama + recommendation
