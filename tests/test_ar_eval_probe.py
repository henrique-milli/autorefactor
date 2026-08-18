"""Deterministic Janus queue probes + sticky-id helpers."""

from __future__ import annotations

from ar_eval.probe import probe_queue_changesets, sticky_changeset_ids


def test_probe_rejects_repo_root_doc_and_padding_ensure() -> None:
    changesets = [
        {
            "id": "doc-root",
            "action": "document",
            "confidence": 0.55,
            "targets": [{"repo": "janus-bonus", "path": ".", "symbol": "janus-bonus"}],
        },
        {
            "id": "doc-file",
            "action": "document",
            "confidence": 0.55,
            "targets": [
                {
                    "repo": "janus-bonus",
                    "path": "src/main/java/Mapper.java",
                    "symbol": "Mapper",
                }
            ],
        },
        {
            "id": "ensure-pad",
            "action": "ensure_tests",
            "confidence": 0.7,
            "targets": [
                {
                    "repo": "janus-bonus",
                    "path": "src/test/java/BonusModelCoverageTest.java",
                    "symbol": "BonusModelCoverageTest",
                }
            ],
        },
    ]
    result = probe_queue_changesets(changesets)
    assert result["ok"] is False
    joined = " ".join(result["failures"])
    assert "repo-root" in joined
    assert "CoverageTest" in joined


def test_probe_rejects_placement_fp() -> None:
    changesets = [
        {
            "id": "c6f9ef",
            "action": "move",
            "title": "Move getBonusFromPamResponse to BonusService",
            "brief": "relocate method",
            "confidence": 0.9,
            "targets": [],
        }
    ]
    result = probe_queue_changesets(changesets)
    assert result["ok"] is False
    assert any("getBonusFromPamResponse" in f for f in result["failures"])


def test_sticky_changeset_ids() -> None:
    prior = {
        "findings": [
            {
                "id": "f1",
                "evidence": [{"changeset_id": "bac2d970"}, {"note": "x"}],
            },
            {"id": "f2", "evidence": [{"changeset_id": "gone"}]},
        ]
    }
    sticky = sticky_changeset_ids(
        prior,
        [{"id": "bac2d970"}, {"id": "new"}],
    )
    assert sticky == ["bac2d970"]


def test_probe_rejects_self_merge_and_missing_coverage_prep() -> None:
    changesets = [
        {
            "id": "merge-self",
            "action": "merge",
            "confidence": 0.99,
            "targets": [
                {"symbol": "com.example.A.fill"},
                {"symbol": "com.example.A.fill"},
            ],
        }
    ]
    result = probe_queue_changesets(changesets, expect_coverage_prep=True)
    assert result["ok"] is False
    joined = " ".join(result["failures"])
    assert "self-merge" in joined
    assert "ensure_tests count is 0" in joined


def test_apply_spec_probes_requires_prep_and_security_hitl() -> None:
    from ar_eval.probe import apply_spec_probes

    spec = {
        "summary": "close coverage prep and secrets HITL",
        "changes": [
            {
                "path": "src/autorefactor/plan/followups.py",
                "acceptance": "ensure_tests ranked before splits",
            },
            {
                "path": "src/autorefactor/mcp/tools.py",
                "acceptance": "committed_secret needs_confirmation security_sensitive",
            },
        ],
    }
    probe = apply_spec_probes(
        {"ok": True, "failures": [], "notes": []},
        [
            {
                "id": "sec1",
                "action": "document",
                "auto_proceed": True,
                "needs_confirmation": False,
                "metrics_delta": {"kind": "committed_secret"},
            }
        ],
        spec,
    )
    assert probe["ok"] is False
    joined = " ".join(probe["failures"])
    assert "ensure_tests count is 0" in joined
    assert "auto_proceed" in joined
    assert probe["ensure_tests_count"] == 0


def test_apply_spec_probes_fails_planner_sticky() -> None:
    from ar_eval.probe import apply_spec_probes

    spec = {
        "changes": [
            {
                "path": "src/autorefactor/plan/followups.py",
                "rationale": "planner still missing prep",
            }
        ]
    }
    probe = apply_spec_probes(
        {"ok": True, "failures": [], "notes": []},
        [{"id": "cs-old", "action": "ensure_tests"}],
        spec,
        planner_unchanged=True,
        sticky_ids=["cs-old"],
    )
    assert probe["ok"] is False
    assert any("sticky_ids" in f for f in probe["failures"])
