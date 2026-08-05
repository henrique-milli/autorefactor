# coderazor.ai Stage 1 design notes

## Product

coderazor.ai is a publishable, Janus-agnostic refactoring agent with **optional standalone stages**:

1. Index / analyze (embed, compare, confirm, report)
2. Optionally write missing in-source docs
3. Optionally write missing tests (coverage + characterization)
4. Plan and apply refactors via OpenHands (no push)

## Key choices

- Deterministic analysis + AnalyzerPlugin adapters (Semgrep, Ruff, ESLint, PMD/SpotBugs, coverage)
- Artifact-backed stage directories under `out/`
- Test harness gate owned by `coderazor test`, not baked into analyze
- OpenHands Software Agent SDK for document / test / refactor agents
- tree-sitter polyglot extractors: Java, TS/TSX, Python, Go, Proto
