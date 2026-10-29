# Product goal

Autorefactor is an **agent-usable refactor queue**, not a click farm.

## In scope

- **High-confidence auto-proceed.** Items at or above the confidence threshold (default 0.6) apply without asking, once `depends_on` is satisfied.
- **HITL only for real judgment:** committed secrets/credentials, architectural wiring (wire vs remove), same-symbol self-merge false positives, MapStruct-sensitive splits, slop that is not a numeric-confidence split (disabled validation, draft in-memory maps), and **true low confidence**.
- **Prep is a phase CTA**, not a click per `ensure_tests`. Ask once whether to raise coverage (and fill docs) before structural refactors. High-confidence prep autoruns; low-confidence prep still confirms.
- **Waiting is not HITL.** Unsatisfied `depends_on` means drain or skip the blocking prep item — do not ask the user to “confirm” a split they cannot apply.
- **Honest report and freshness.** Stale runs must not look current. Coverage-first is queue **order** plus `depends_on`, not a confirm gate on every structural item.

## Out of scope

- Extra always-HITL gates for cosmetic flavor, “never autorun,” or “never auto_proceed `ensure_tests`.”
- Confidence caps as a substitute for judgment kinds.
- New sensors without a product-surface miss (report/queue/MCP).
- Subject-repo refactors, playbook essays, and sticky changeset-id chasing.
