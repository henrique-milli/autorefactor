"""Unit tests for self-improve loop helpers (no live Cursor)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from ar_eval.allowlist import (
    filter_allowed_changes,
    is_allowed_alpha_path,
    rank_and_budget_changes,
)
from ar_eval.corpus import Episode
from ar_eval.evidence import changed_files, verify_lifecycle_evidence, write_snapshot
from ar_eval.engine import analyze_run_slug, fetch_changesets_for_slug, reset_for_episode
from ar_eval.loop import (
    _agents_timing,
    _critic_prompt,
    _iteration_timing_payload,
    _write_iteration_timing_json,
    load_user_persona,
    repair_failed_smoke,
    select_episodes_for_iteration,
    select_persona,
    _smoke_fix_prompt,
)
from ar_eval.rebuild import compose_rebuild_cmd, rebuild_engine
from ar_eval.retest import score_retest
from ar_eval.tool_audit import audit_mcp_tools


def test_select_episodes_cycles() -> None:
    eps = [
        Episode(id="a", roots=["x"]),
        Episode(id="b", roots=["y"]),
        Episode(id="c", roots=["z"]),
    ]
    assert select_episodes_for_iteration(eps, iteration=1, all_episodes=False)[0].id == "a"
    assert select_episodes_for_iteration(eps, iteration=2, all_episodes=False)[0].id == "b"
    assert select_episodes_for_iteration(eps, iteration=4, all_episodes=False)[0].id == "a"


def test_select_sticky_episode() -> None:
    eps = [Episode(id="a", roots=["x"]), Episode(id="b", roots=["y"])]
    out = select_episodes_for_iteration(
        eps, iteration=2, all_episodes=False, sticky_episode_id="a"
    )
    assert [e.id for e in out] == ["a"]


def test_select_episodes_all_reorders_sticky() -> None:
    eps = [
        Episode(id="a", roots=["x"]),
        Episode(id="b", roots=["y"]),
        Episode(id="c", roots=["z"]),
    ]
    out = select_episodes_for_iteration(
        eps, iteration=1, all_episodes=True, sticky_episode_id="c"
    )
    assert [e.id for e in out] == ["c", "a", "b"]


def test_merge_feedback_tags_persona_and_episode() -> None:
    from ar_eval.loop import _merge_feedback_blobs

    merged = _merge_feedback_blobs(
        [
            {
                "persona": "coverage_purist",
                "episode_id": "ep-a",
                "findings": [{"id": "f1", "title": "hitl", "severity": "high"}],
            },
            {
                "persona": "semantics_maximalist",
                "episode_id": "ep-b",
                "findings": [{"id": "f2", "title": "miss", "severity": "medium"}],
            },
        ],
        iteration=2,
        product_use_ok=True,
    )
    assert merged["episode_id"] == "iter-2"
    assert len(merged["findings"]) == 2
    assert merged["findings"][0]["persona"] == "coverage_purist"
    assert merged["findings"][0]["episode_id"] == "ep-a"
    assert merged["findings"][0]["local_id"] == "f1"
    assert merged["findings"][0]["id"] == "ep-a:coverage_purist:f1"
    assert merged["findings"][1]["id"] == "ep-b:semantics_maximalist:f2"
    assert merged["findings"][1]["persona"] == "semantics_maximalist"
    assert [w["persona"] for w in merged["walks"]] == [
        "coverage_purist",
        "semantics_maximalist",
    ]


def test_select_persona_rotates() -> None:
    assert select_persona(1) == "user_product_critic.md"
    assert select_persona(2) == "user_coverage_purist.md"
    assert select_persona(3) == "user_confidence_skeptic.md"
    assert select_persona(4) == "user_semantics_maximalist.md"
    assert select_persona(5) == "user_product_critic.md"


def test_load_user_persona_prepends_lifecycle(tmp_path: Path) -> None:
    personas = tmp_path / "personas"
    personas.mkdir()
    (personas / "user_product_critic.md").write_text(
        "# base\nUse get_report\n", encoding="utf-8"
    )
    (personas / "user_coverage_purist.md").write_text(
        "Bias: coverage\n", encoding="utf-8"
    )
    eval_dir = tmp_path
    text = load_user_persona(eval_dir, "user_coverage_purist.md")
    assert "Use get_report" in text
    assert "Bias: coverage" in text
    assert "Persona bias" in text


def test_load_semantics_persona_overlay(tmp_path: Path) -> None:
    personas = tmp_path / "personas"
    personas.mkdir()
    (personas / "user_product_critic.md").write_text(
        "# base\nUse get_report\n", encoding="utf-8"
    )
    (personas / "user_semantics_maximalist.md").write_text(
        "Bias: embeddings and semantics\n"
        'Set `"persona": "semantics_maximalist"` in `feedback.json`.\n',
        encoding="utf-8",
    )
    text = load_user_persona(tmp_path, "user_semantics_maximalist.md")
    assert "Use get_report" in text
    assert "Bias: embeddings and semantics" in text
    assert '"persona": "semantics_maximalist"' in text


def test_allowlist_paths() -> None:
    assert is_allowed_alpha_path("src/autorefactor/mcp/guidance.py")
    assert is_allowed_alpha_path("tests/test_report_stale_gate.py")
    assert not is_allowed_alpha_path(".env")
    assert not is_allowed_alpha_path("eval/subjects/demo/foo.py")


def test_rank_and_budget_changes() -> None:
    changes = [
        {"path": "src/autorefactor/a.py", "rationale": "f2 low", "acceptance": "x"},
        {"path": "src/autorefactor/b.py", "rationale": "f1 friction", "acceptance": "x"},
        {"path": "src/autorefactor/c.py", "rationale": "f3", "acceptance": "x"},
        {"path": "src/autorefactor/d.py", "rationale": "f4", "acceptance": "x"},
        {"path": ".env", "rationale": "f1", "acceptance": "x"},
    ]
    meta = {
        "f1": {"severity": "critical", "kind": "friction"},
        "f2": {"severity": "low", "kind": "other"},
        "f3": {"severity": "high", "kind": "miss"},
        "f4": {"severity": "medium", "kind": "other"},
    }
    out = rank_and_budget_changes(changes, max_changes=3, finding_meta=meta)
    assert len(out) == 3
    assert out[0]["path"].endswith("b.py")
    assert all(is_allowed_alpha_path(c["path"]) for c in out)


def test_filter_allowed_changes() -> None:
    kept, rejected = filter_allowed_changes(
        [
            {"path": "src/autorefactor/cli.py", "rationale": "r", "acceptance": "a"},
            {"path": ".env", "rationale": "r", "acceptance": "a"},
        ]
    )
    assert len(kept) == 1
    assert rejected == [".env"]


def _good_feedback(*, tools: list[str] | None = None) -> dict:
    return {
        "source": "product_use",
        "lifecycle": {
            "report_reviewed": True,
            "auto_proceed_applied": 0,
            "hitl_sampled": 2,
            "hitl_proceeded": 0,
            "hitl_skipped": 0,
            "hitl_burden_pending": 4,
            "queue_sampled_ids": ["c1"],
            "subject_files_edited": [],
            "mcp_tools_used": tools
            or [
                "get_status",
                "get_report",
                "sample_queue",
            ],
        },
        "findings": [
            {
                "id": "f1",
                "severity": "high",
                "title": "t",
                "kind": "friction",
                "product_surface": "queue",
                "evidence": [{"changeset_id": "c1", "note": "sample_queue hitl_digest"}],
            }
        ],
    }


def test_evidence_rejects_subject_edits(tmp_path: Path) -> None:
    ws = tmp_path / "ep"
    ws.mkdir()
    f = ws / "a.py"
    f.write_text("x=1\n", encoding="utf-8")
    write_snapshot(ws)
    fb = _good_feedback()
    good = verify_lifecycle_evidence(fb, workspace=ws)
    assert good["ok"] is True
    f.write_text("x=2\n", encoding="utf-8")
    assert "a.py" in changed_files(ws)
    bad = verify_lifecycle_evidence(fb, workspace=ws)
    assert bad["ok"] is False
    assert "must_not_edit_subject" in bad["reasons"]


def test_tool_audit_requires_core_tools() -> None:
    bad = audit_mcp_tools(
        {
            "lifecycle": {
                "hitl_sampled": 1,
                "mcp_tools_used": ["get_status"],
            }
        }
    )
    assert bad["ok"] is False
    assert "mcp_tools_missing_required" in bad["reasons"]

    good = audit_mcp_tools(_good_feedback())
    assert good["ok"] is True

    fallback = audit_mcp_tools(
        _good_feedback(tools=["get_status", "get_report", "list_changesets", "get_changeset"])
    )
    assert fallback["ok"] is True


def test_tool_audit_rejects_queue_mutation() -> None:
    bad = audit_mcp_tools(
        _good_feedback(
            tools=["get_status", "get_report", "sample_queue", "confirm_changeset"]
        )
    )
    assert bad["ok"] is False
    assert "must_not_mutate_queue" in bad["reasons"]


def test_evidence_fails_without_tools(tmp_path: Path) -> None:
    ws = tmp_path / "ep"
    ws.mkdir()
    f = ws / "a.py"
    f.write_text("x=1\n", encoding="utf-8")
    write_snapshot(ws)
    fb = _good_feedback(tools=["get_status"])
    out = verify_lifecycle_evidence(fb, workspace=ws)
    assert out["ok"] is False
    assert "mcp_tools_missing_required" in out["reasons"]


def test_score_retest() -> None:
    prior = [{"id": "f1"}, {"id": "f2"}]
    retest = {
        "results": [
            {"id": "f1", "status": "resolved"},
            {"id": "f2", "status": "partial"},
        ]
    }
    scored = score_retest(retest, prior)
    assert scored["total"] == 2
    assert scored["resolved"] == 1
    assert scored["score"] == 0.75


def test_compose_rebuild_cmd() -> None:
    assert compose_rebuild_cmd() == [
        "docker",
        "compose",
        "restart",
        "autorefactor",
    ]


def test_dockerfile_installs_setuptools_before_no_build_isolation() -> None:
    """Editable reinstall uses --no-build-isolation; setuptools must already be in the image."""
    text = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text(encoding="utf-8")
    isol = text.find("--no-build-isolation")
    assert isol != -1
    setup = text.find("setuptools")
    assert setup != -1
    assert setup < isol


def test_ensure_engine_dry_run(tmp_path: Path) -> None:
    from ar_eval.rebuild import ensure_engine

    result = ensure_engine(tmp_path, dry_run=True)
    assert result["ok"] is True
    assert result["cmd"] == [
        "docker",
        "compose",
        "up",
        "-d",
        "--build",
        "autorefactor",
    ]


def test_rebuild_engine_dry_run(tmp_path: Path) -> None:
    result = rebuild_engine(tmp_path, dry_run=True)
    assert result["ok"] is True
    assert result["dry_run"] is True
    assert result["cmd"] == [
        "docker",
        "compose",
        "restart",
        "autorefactor",
    ]


def test_rebuild_engine_runs_subprocess(tmp_path: Path) -> None:
    with (
        patch("ar_eval.rebuild.subprocess.run") as run,
        patch("ar_eval.rebuild.wait_healthy", return_value={"ok": True}),
    ):
        out = rebuild_engine(tmp_path, engine_url="http://127.0.0.1:8765")
    run.assert_called_once()
    assert run.call_args[0][0] == [
        "docker",
        "compose",
        "restart",
        "autorefactor",
    ]
    assert out["ok"] is True
    assert "compose_restart_s" in out["timing"]


def test_reset_for_episode_includes_timing_schema(tmp_path: Path) -> None:
    with (
        patch("ar_eval.engine.wait_healthy", return_value={"ok": True}),
        patch("ar_eval.engine.sync_episode", return_value={"synced": True}),
        patch(
            "ar_eval.engine.analyze_active_workspace",
            return_value={"job": {"slug": "x"}, "status": "done"},
        ),
    ):
        out = reset_for_episode(
            tmp_path,
            engine_url="http://127.0.0.1:8765",
            label="lbl",
            restart=False,
            sync_retries=1,
            analyze=True,
            analyze_skip_llm=True,
            analyze_timeout=1.0,
        )

    assert out["ok"] is True
    assert isinstance(out["timing"], dict)
    assert "initial_health_wait_s" in out["timing"]
    assert "analyze_s" in out["timing"]
    assert isinstance(out["timing"]["initial_health_wait_s"], float)
    assert isinstance(out["timing"]["analyze_s"], float)
    assert isinstance(out["timing"]["sync_attempts"], list)
    assert len(out["timing"]["sync_attempts"]) == 1
    assert out["timing"]["sync_attempts"][0]["attempt"] == 0
    assert "sync_episode_s" in out["timing"]["sync_attempts"][0]
    assert isinstance(out["timing"]["sync_attempts"][0]["sync_episode_s"], float)


def test_iteration_timing_json_schema_written(tmp_path: Path) -> None:
    iter_dir = tmp_path / "iter-001"
    iter_dir.mkdir(parents=True)
    payload = _iteration_timing_payload(
        iteration=1,
        stim_results=[
            {
                "episode_id": "ep1",
                "engine": {"timing": {"sync_s": 0.1}},
                "walks": [{"persona_stem": "product_critic", "critic_s": 1.5}],
                "critics_wall_s": 1.6,
            },
            {
                "episode_id": "ep2",
                "engine": {"timing": {"sync_s": 0.05}},
                "critics_wall_s": 2.0,
            },
        ],
        retest_results=[
            {
                "episode_id": "ep1",
                "engine": {"timing": {"retest_s": 0.2}},
                "probe": {"timing": {"probe_queue_changesets_s": 0.3}},
                "agent": {"duration_s": 2.2},
            },
            {
                "episode_id": "ep2",
                "engine": {"timing": {"retest_s": 0.4}},
                "probe": {"timing": {"probe_queue_changesets_s": 0.01}},
            },
        ],
        rebuild_info={"timing": {"compose_restart_s": 0.4}},
        ensure_info=None,
        agents=_agents_timing(
            stim_results=[{"critics_wall_s": 1.6}, {"critics_wall_s": 2.0}],
            triage={"duration_s": 0.8},
            alpha={"duration_s": 12.0, "timed_out": False},
            retest_info={"agent": {"duration_s": 2.2}},
        ),
    )
    _write_iteration_timing_json(iter_dir, payload)

    timing_path = iter_dir / "timing.json"
    assert timing_path.is_file()
    data = json.loads(timing_path.read_text(encoding="utf-8"))
    assert data["iteration"] == 1
    assert data["rebuild"] == {"compose_restart_s": 0.4}
    assert data["episodes"]["ep1"]["stimulus"]["timing"]["sync_s"] == 0.1
    assert data["episodes"]["ep1"]["stimulus"]["critics_wall_s"] == 1.6
    assert data["episodes"]["ep1"]["stimulus"]["walks"][0]["critic_s"] == 1.5
    assert (
        data["episodes"]["ep1"]["retest"]["probe_timing"]["probe_queue_changesets_s"]
        == 0.3
    )
    assert data["episodes"]["ep2"]["retest"]["timing"]["retest_s"] == 0.4
    assert data["agents"]["triage_s"] == 0.8
    assert data["agents"]["alpha_s"] == 12.0
    assert data["agents"]["alpha_timed_out"] is False
    assert data["agents"]["critics_wall_s"] == 3.6
    assert data["agents"]["retest_agent_s"] == 2.2


def test_run_retest_episode_attaches_probe_timing(tmp_path: Path) -> None:
    from ar_eval.loop import run_retest_episode

    episode = Episode(id="ep1", roots=["rootA"])
    repo_root = tmp_path / "repo"
    eval_dir = tmp_path / "eval"
    art_dir = eval_dir / "artifacts" / "loop" / "iter-001" / "ep1"
    repo_root.mkdir(parents=True)
    eval_dir.mkdir(parents=True)
    art_dir.mkdir(parents=True)

    prior_feedback_path = art_dir / "prior_feedback.json"
    prior_feedback_path.write_text(
        json.dumps({"findings": [{"id": "f1"}]}),
        encoding="utf-8",
    )

    def _fake_materialize_episode(*_args, **_kwargs) -> Path:
        ws = tmp_path / "ws"
        ws.mkdir(parents=True, exist_ok=True)
        return ws

    class _NoopLock:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def __enter__(self):
            return None

        def __exit__(self, *_exc) -> None:
            return None

    with (
        patch("ar_eval.loop.materialize_episode", side_effect=_fake_materialize_episode),
        patch("ar_eval.loop.EngineLock", _NoopLock),
        patch(
            "ar_eval.loop.reset_for_episode",
            return_value={
                "ok": True,
                "sync": {"ok": True},
                "restarted": True,
                "analyze": {"slug": "run-slug-1", "status": "done"},
                "timing": {"initial_health_wait_s": 0.01, "analyze_s": 0.02},
            },
        ),
        patch("ar_eval.loop.fetch_changesets_for_slug", return_value=("run-slug-1", [{"id": "cs1"}])),
        patch("ar_eval.loop.probe_queue_changesets", return_value={
            "ok": True,
            "failures": [],
            "notes": [],
            "counts": {"changesets": 1, "documents": 0, "root_documents": 0},
        }),
        patch("ar_eval.loop.run_local_agent") as agent,
    ):
        out = run_retest_episode(
            episode,
            repo_root=repo_root,
            eval_dir=eval_dir,
            art_dir=art_dir,
            engine_url="http://127.0.0.1:8765",
            model="composer-2.5",
            prior_feedback_path=prior_feedback_path,
            iteration=1,
            analyze_skip_llm=True,
            analyze_timeout=1.0,
            progress=None,
        )

    agent.assert_not_called()
    assert out["ok"] is True
    assert out["passed"] is True
    assert out["agent"] is None
    assert out["score"]["mode"] == "probe"
    assert out["probe"]["timing"]
    assert "fetch_changesets_for_slug_s" in out["probe"]["timing"]
    assert "probe_queue_changesets_s" in out["probe"]["timing"]


def test_analyze_run_slug_prefers_exact_slug_fields() -> None:
    payload = {
        "slug": "top-level-slug",
        "job": {
            "slug": "job-slug",
            "result": {
                "slug": "result-slug",
                "out_dir": "/tmp/runs/result-slug",
            },
        },
    }
    assert analyze_run_slug(payload) == "result-slug"


def test_analyze_run_slug_falls_back_to_out_dir_name() -> None:
    payload = {
        "out_dir": "/tmp/runs/top-level",
        "job": {
            "result": {
                "out_dir": "/tmp/runs/janus-account-1234567890",
            }
        },
    }
    assert analyze_run_slug(payload) == "janus-account-1234567890"


def test_fetch_changesets_for_slug_uses_exact_run_slug() -> None:
    with patch("ar_eval.engine._get_json") as get_json:
        get_json.return_value = {
            "changesets": [
                {"id": "cs1", "action": "ensure_tests"},
                "skip-me",
            ]
        }
        slug, items = fetch_changesets_for_slug(
            url="http://127.0.0.1:8765",
            slug="janus-account-1234567890",
        )

    get_json.assert_called_once_with(
        "http://127.0.0.1:8765/v1/runs/janus-account-1234567890/changesets",
        timeout=60.0,
    )
    assert slug == "janus-account-1234567890"
    assert items == [{"id": "cs1", "action": "ensure_tests"}]


def test_cli_loop_help() -> None:
    from ar_eval.waves import main

    with pytest.raises(SystemExit) as ei:
        main(["loop", "--help"])
    assert ei.value.code == 0


def test_cli_loop_json_and_no_progress_flags(capsys) -> None:
    from ar_eval.waves import _build_parser, main

    args = _build_parser().parse_args(["loop", "--json", "--no-progress"])
    assert args.json is True
    assert args.no_progress is True
    with pytest.raises(SystemExit) as ei:
        main(["loop", "--help"])
    assert ei.value.code == 0
    out = capsys.readouterr().out
    assert "--json" in out
    assert "--no-progress" in out
    assert "--alpha-timeout" in out
    assert "--retest-agent" in out
    assert "persona" in out.lower()
    assert "spec-probe" in out.lower() or "every episode" in out.lower()


def test_progress_line_mode_and_summary() -> None:
    from io import StringIO

    from ar_eval.progress import LoopProgress, format_loop_summary

    buf = StringIO()
    progress = LoopProgress(file=buf, force_lines=True)
    progress.start(1)
    progress.set_context(
        iteration=1, episode="janus-bonus", persona="user_product_critic.md"
    )
    progress.phase("ensure", "compose up")
    progress.heartbeat("health ok")
    progress.finish_iteration(
        {"iteration": 1, "user": "ok", "smoke": "pass", "retest": "0.80 pass"}
    )
    progress.stop()
    text = buf.getvalue()
    assert "ensure" in text
    assert "janus-bonus" in text
    assert "iter-done" in text or "user ok" in text

    summary_text = format_loop_summary(
        {
            "iterations": 1,
            "results": [
                {
                    "iteration": 1,
                    "persona": "user_product_critic.md",
                    "skipped_agents": False,
                    "stimulus": [{"feedback_gate": {"product_use_ok": True}}],
                    "smoke": {"ok": True},
                    "retests": [
                        {"episode_id": "ep1", "passed": True},
                        {"episode_id": "ep2", "passed": True},
                    ],
                    "retest": {
                        "passed": True,
                        "episodes_passed": 2,
                        "episodes_total": 2,
                    },
                }
            ],
        },
        artifacts="eval/artifacts/loop/summary.json",
    )
    assert "ar-eval loop done" in summary_text
    assert "user=ok" in summary_text
    assert "retest=2/2 pass" in summary_text
    assert "0.80" not in summary_text
    assert "eval/artifacts/loop/summary.json" in summary_text


def test_progress_live_init_on_tty() -> None:
    """Live() renders immediately; attributes must exist before that call."""
    from io import StringIO

    from ar_eval.progress import LoopProgress

    class _Tty(StringIO):
        def isatty(self) -> bool:
            return True

    progress = LoopProgress(file=_Tty(), force_lines=False)
    assert progress.iterations == 0
    rendered = progress._render()
    assert rendered is not None
    progress.start(2)
    assert progress.iterations == 2
    progress.stop()


def test_engine_lock(tmp_path: Path) -> None:
    from ar_eval.engine_lock import EngineLock

    lock = tmp_path / "engine.lock"
    with EngineLock(lock, timeout=2.0):
        assert lock.is_file()
        contents = lock.read_text(encoding="utf-8")
        assert "pid=" in contents


def test_smoke_fix_prompt_includes_traceback() -> None:
    prompt = _smoke_fix_prompt(
        persona="alpha",
        iteration=2,
        smoke={
            "ok": False,
            "returncode": 1,
            "cmd": ["uv", "run", "pytest", "-q"],
            "stdout_tail": "FAILED tests/test_jobs.py::test_x",
            "stderr_tail": "AssertionError: boom",
        },
    )
    assert "FAILED tests/test_jobs.py::test_x" in prompt
    assert "AssertionError: boom" in prompt
    assert "smoke_repair" in prompt


def test_repair_failed_smoke_skips_when_green(tmp_path: Path) -> None:
    eval_dir = tmp_path / "eval"
    (eval_dir / "personas").mkdir(parents=True)
    (eval_dir / "personas" / "alpha_improver.md").write_text("alpha\n", encoding="utf-8")
    iter_dir = tmp_path / "iter"
    iter_dir.mkdir()
    calls: list[str] = []

    def agent(**kwargs):
        calls.append("agent")
        return {"ok": True}

    smoke, repairs = repair_failed_smoke(
        repo_root=tmp_path,
        eval_dir=eval_dir,
        iter_dir=iter_dir,
        iteration=1,
        model="composer-2.5",
        smoke={"ok": True, "returncode": 0},
        run_agent=agent,
        run_tests=lambda _root: {"ok": True},
    )
    assert smoke["ok"] is True
    assert repairs == []
    assert calls == []


def test_repair_failed_smoke_reruns_until_green(tmp_path: Path) -> None:
    eval_dir = tmp_path / "eval"
    (eval_dir / "personas").mkdir(parents=True)
    (eval_dir / "personas" / "alpha_improver.md").write_text("alpha\n", encoding="utf-8")
    iter_dir = tmp_path / "iter"
    iter_dir.mkdir()
    prompts: list[str] = []

    def agent(**kwargs):
        prompts.append(kwargs["prompt"])
        return {"ok": True, "status": "finished"}

    tests = iter(
        [
            {"ok": True, "returncode": 0, "stdout_tail": "passed"},
        ]
    )

    smoke, repairs = repair_failed_smoke(
        repo_root=tmp_path,
        eval_dir=eval_dir,
        iter_dir=iter_dir,
        iteration=2,
        model="composer-2.5",
        smoke={
            "ok": False,
            "returncode": 1,
            "cmd": ["pytest"],
            "stdout_tail": "FAILED tests/test_jobs.py",
            "stderr_tail": "",
        },
        run_agent=agent,
        run_tests=lambda _root: next(tests),
    )
    assert len(repairs) == 1
    assert smoke["ok"] is True
    assert "FAILED tests/test_jobs.py" in prompts[0]
    assert (iter_dir / "smoke_repair_01.json").is_file()
    assert (iter_dir / "smoke.json").is_file()


def test_run_stimulus_critic_parallel_walks(tmp_path: Path) -> None:
    from ar_eval.loop import USER_PERSONAS, run_stimulus_critic

    episode = Episode(id="ep1", roots=["rootA"])
    repo_root = tmp_path / "repo"
    eval_dir = tmp_path / "eval"
    art_dir = eval_dir / "artifacts" / "loop" / "iter-001" / "ep1"
    repo_root.mkdir()
    personas = eval_dir / "personas"
    personas.mkdir(parents=True)
    (personas / "user_product_critic.md").write_text("# base\n", encoding="utf-8")
    for name in USER_PERSONAS:
        if name == "user_product_critic.md":
            continue
        (personas / name).write_text(f"bias {name}\n", encoding="utf-8")
    art_dir.mkdir(parents=True)
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.py").write_text("x=1\n", encoding="utf-8")

    class _NoopLock:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def __enter__(self):
            return None

        def __exit__(self, *_exc) -> None:
            return None

    def _fake_agent(*, prompt: str, cwd: Path, **_kwargs) -> dict[str, Any]:
        import re

        match = re.search(r"Write `feedback.json` to this exact path: `([^`]+)`", prompt)
        assert match
        dest = Path(match.group(1))
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(
            json.dumps(
                {
                    "source": "product_use",
                    "persona": dest.parent.name,
                    "episode_id": "ep1",
                    "lifecycle": {
                        "report_reviewed": True,
                        "hitl_sampled": 2,
                        "mcp_tools_used": ["get_status", "get_report", "sample_queue"],
                    },
                    "findings": [
                        {
                            "id": f"f-{dest.parent.name}",
                            "severity": "high",
                            "title": "hitl burden",
                            "kind": "friction",
                            "product_surface": "queue",
                            "evidence": [{"changeset_id": "c1", "note": "sample_queue"}],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        return {"ok": True, "status": "finished"}

    with (
        patch("ar_eval.loop.materialize_episode", return_value=ws),
        patch("ar_eval.loop.EngineLock", _NoopLock),
        patch(
            "ar_eval.loop.reset_for_episode",
            return_value={
                "ok": True,
                "sync": {"ok": True},
                "analyze": {"slug": "run-1", "status": "done"},
                "timing": {"analyze_s": 0.01},
            },
        ),
        patch("ar_eval.loop.run_local_agent", side_effect=_fake_agent),
    ):
        out = run_stimulus_critic(
            episode,
            repo_root=repo_root,
            eval_dir=eval_dir,
            art_dir=art_dir,
            engine_url="http://127.0.0.1:8765",
            model="composer-2.5",
            materialize_mode="copy",
            skip_agents=False,
            analyze=True,
            persona_files=list(USER_PERSONAS),
        )

    assert out["feedback_gate"]["product_use_ok"] is True
    assert len(out["walks"]) == len(USER_PERSONAS)
    assert out["critics_wall_s"] is not None
    assert out["critics_wall_s"] >= 0
    assert all(w.get("critic_s") is not None for w in out["walks"])
    merged = json.loads((art_dir / "feedback.json").read_text(encoding="utf-8"))
    assert len(merged["findings"]) == len(USER_PERSONAS)
    stems = {f["persona"] for f in merged["findings"]}
    assert "product_critic" in stems
    assert "semantics_maximalist" in stems


def _retest_noop_lock():
    class _NoopLock:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def __enter__(self):
            return None

        def __exit__(self, *_exc) -> None:
            return None

    return _NoopLock


def test_run_retest_episode_probe_failure_sets_passed_false(tmp_path: Path) -> None:
    from ar_eval.loop import run_retest_episode

    episode = Episode(id="ep1", roots=["rootA"])
    repo_root = tmp_path / "repo"
    eval_dir = tmp_path / "eval"
    art_dir = eval_dir / "artifacts" / "loop" / "iter-001" / "ep1"
    repo_root.mkdir(parents=True)
    eval_dir.mkdir(parents=True)
    art_dir.mkdir(parents=True)
    prior = art_dir / "prior_feedback.json"
    prior.write_text(json.dumps({"findings": [{"id": "f1"}]}), encoding="utf-8")
    ws = tmp_path / "ws"
    ws.mkdir()

    with (
        patch("ar_eval.loop.materialize_episode", return_value=ws),
        patch("ar_eval.loop.EngineLock", _retest_noop_lock()),
        patch(
            "ar_eval.loop.reset_for_episode",
            return_value={
                "ok": True,
                "analyze": {"slug": "run-slug-1", "status": "done"},
                "timing": {"analyze_s": 0.02},
            },
        ),
        patch("ar_eval.loop.fetch_changesets_for_slug", return_value=("run-slug-1", [])),
        patch(
            "ar_eval.loop.probe_queue_changesets",
            return_value={"ok": False, "failures": ["empty_queue"], "notes": []},
        ),
        patch("ar_eval.loop.run_local_agent") as agent,
    ):
        out = run_retest_episode(
            episode,
            repo_root=repo_root,
            eval_dir=eval_dir,
            art_dir=art_dir,
            engine_url="http://127.0.0.1:8765",
            model="composer-2.5",
            prior_feedback_path=prior,
            iteration=1,
        )

    agent.assert_not_called()
    assert out["passed"] is False
    assert out["score"]["probe_ok"] is False
    assert out["score"]["mode"] == "probe"


def test_run_retest_episode_opt_in_agent(tmp_path: Path) -> None:
    from ar_eval.loop import run_retest_episode

    episode = Episode(id="ep1", roots=["rootA"])
    repo_root = tmp_path / "repo"
    eval_dir = tmp_path / "eval"
    art_dir = eval_dir / "artifacts" / "loop" / "iter-001" / "ep1"
    repo_root.mkdir(parents=True)
    personas = eval_dir / "personas"
    personas.mkdir(parents=True)
    (personas / "user_retest.md").write_text("# retest\n", encoding="utf-8")
    art_dir.mkdir(parents=True)
    prior = art_dir / "prior_feedback.json"
    prior.write_text(json.dumps({"findings": [{"id": "f1"}]}), encoding="utf-8")
    ws = tmp_path / "ws"
    ws.mkdir()

    def _fake_agent(*, cwd: Path, **_kwargs) -> dict[str, Any]:
        (cwd / "retest.json").write_text(
            json.dumps({"results": [{"id": "f1", "status": "resolved"}]}),
            encoding="utf-8",
        )
        return {"ok": True, "status": "finished", "duration_s": 0.4}

    with (
        patch("ar_eval.loop.materialize_episode", return_value=ws),
        patch("ar_eval.loop.EngineLock", _retest_noop_lock()),
        patch(
            "ar_eval.loop.reset_for_episode",
            return_value={
                "ok": True,
                "analyze": {"slug": "run-slug-1", "status": "done"},
                "timing": {"analyze_s": 0.02},
            },
        ),
        patch("ar_eval.loop.fetch_changesets_for_slug", return_value=("run-slug-1", [{"id": "cs1"}])),
        patch(
            "ar_eval.loop.probe_queue_changesets",
            return_value={"ok": True, "failures": [], "notes": []},
        ),
        patch("ar_eval.loop.run_local_agent", side_effect=_fake_agent) as agent,
    ):
        out = run_retest_episode(
            episode,
            repo_root=repo_root,
            eval_dir=eval_dir,
            art_dir=art_dir,
            engine_url="http://127.0.0.1:8765",
            model="composer-2.5",
            prior_feedback_path=prior,
            iteration=1,
            run_retest_agent=True,
        )

    agent.assert_called_once()
    assert out["passed"] is True
    assert out["agent"]["duration_s"] == 0.4
    assert out["score"]["mode"] == "agent"
    assert out["score"]["llm_passed"] is True


def test_run_retest_episode_spec_probes_fail_without_agent(tmp_path: Path) -> None:
    from ar_eval.loop import run_retest_episode

    episode = Episode(id="ep1", roots=["rootA"])
    repo_root = tmp_path / "repo"
    eval_dir = tmp_path / "eval"
    art_dir = eval_dir / "artifacts" / "loop" / "iter-001" / "ep1"
    repo_root.mkdir(parents=True)
    eval_dir.mkdir(parents=True)
    art_dir.mkdir(parents=True)
    prior = art_dir / "prior_feedback.json"
    prior.write_text(
        json.dumps(
            {"findings": [{"id": "f1", "evidence": [{"changeset_id": "sticky1"}]}]}
        ),
        encoding="utf-8",
    )
    ws = tmp_path / "ws"
    ws.mkdir()
    spec = {
        "changes": [
            {
                "path": "src/autorefactor/plan/followups.py",
                "acceptance": "ensure_tests >= 1 before splits",
            }
        ]
    }

    with (
        patch("ar_eval.loop.materialize_episode", return_value=ws),
        patch("ar_eval.loop.EngineLock", _retest_noop_lock()),
        patch(
            "ar_eval.loop.reset_for_episode",
            return_value={
                "ok": True,
                "analyze": {"slug": "run-slug-1", "status": "done"},
            },
        ),
        patch("ar_eval.loop.fetch_changesets_for_slug", return_value=("run-slug-1", [])),
        patch(
            "ar_eval.loop.probe_queue_changesets",
            return_value={"ok": True, "failures": [], "notes": []},
        ),
        patch("ar_eval.loop.run_local_agent") as agent,
    ):
        out = run_retest_episode(
            episode,
            repo_root=repo_root,
            eval_dir=eval_dir,
            art_dir=art_dir,
            engine_url="http://127.0.0.1:8765",
            model="composer-2.5",
            prior_feedback_path=prior,
            iteration=1,
            spec=spec,
        )

    agent.assert_not_called()
    assert out["passed"] is False
    assert out["score"]["mode"] == "probe"
    joined = " ".join(out["probe"].get("failures") or [])
    assert "ensure_tests count is 0" in joined


def test_iteration_row_uses_all_episode_retest_counts() -> None:
    from ar_eval.loop import _iteration_row

    row = _iteration_row(
        {
            "iteration": 1,
            "stimulus": [{"feedback_gate": {"product_use_ok": True}}],
            "smoke": {"ok": True},
            "retests": [
                {"episode_id": "a", "passed": True},
                {"episode_id": "b", "passed": False},
            ],
            "retest": {"score": {"score": 1.0}, "passed": True},
        }
    )
    assert row["retest"] == "1/2 fail"


def test_critic_prompt_is_goal_oriented(tmp_path: Path) -> None:
    text = _critic_prompt(
        episode_id="ep1",
        persona="# overlay",
        engine_url="http://127.0.0.1:8765",
        feedback_path=tmp_path / "feedback.json",
        prior_findings=[
            {"id": "f1", "local_id": "f1", "title": "ensure_tests miss", "kind": "miss"}
        ],
        prior_spec={
            "changes": [{"path": "src/autorefactor/plan/followups.py", "acceptance": "prep"}]
        },
    )
    lower = text.lower()
    assert "mission" in lower
    assert "success" in lower
    assert "sample_queue" in text
    assert "get_status" in text
    assert "trust" in lower
    assert "feedback.json" in text
    assert "never run" not in lower
    assert "still_open" in lower
    assert "ensure_tests miss" in text
    assert "followups.py" in text
    for ban in ("`mvn", "`pytest", "`npm", "mvn test", "npm test"):
        assert ban not in lower


def test_personas_are_goal_oriented() -> None:
    root = Path(__file__).resolve().parents[1]
    files = [
        root / "eval" / "personas" / "user_product_critic.md",
        root / "eval" / "personas" / "user_retest.md",
        root / "eval" / "personas" / "triage_alpha.md",
        root / "eval" / "personas" / "alpha_improver.md",
    ]
    for path in files:
        text = path.read_text(encoding="utf-8")
        lower = text.lower()
        assert "mission" in lower or "success" in lower or "out of scope" in lower
        for ban in ("never run `mvn", "never run `pytest", "never run `npm", "`mvn", "`npm"):
            assert ban not in lower
        assert "mvn test" not in lower
    critic = (root / "eval" / "personas" / "user_product_critic.md").read_text(
        encoding="utf-8"
    )
    assert "sample_queue" in critic or "sample walk" in critic.lower()
    assert "get_status" in critic
    assert "already" in critic.lower()


def test_alpha_timeout_continues_to_smoke(tmp_path: Path) -> None:
    from ar_eval.loop import _run_self_improve_loop_body

    repo = tmp_path / "repo"
    eval_dir = tmp_path / "eval"
    (eval_dir / "personas").mkdir(parents=True)
    (eval_dir / "personas" / "triage_alpha.md").write_text("triage\n", encoding="utf-8")
    (eval_dir / "personas" / "alpha_improver.md").write_text("alpha\n", encoding="utf-8")
    repo.mkdir()
    episode = Episode(id="ep1", roots=["r"])
    fb_dir = eval_dir / "artifacts" / "loop" / "iter-001" / "ep1"
    fb_dir.mkdir(parents=True)
    fb_path = fb_dir / "feedback.json"
    fb_path.write_text(
        json.dumps(
            {
                "source": "product_use",
                "findings": [
                    {"id": "f1", "severity": "high", "kind": "friction"}
                ],
                "lifecycle": {
                    "report_reviewed": True,
                    "hitl_sampled": 1,
                    "mcp_tools_used": ["get_status"],
                },
            }
        ),
        encoding="utf-8",
    )
    roles: list[str] = []

    def fake_agent(*, prompt: str, cwd: Path, timeout_s=None, **_kwargs):
        if "sample-walk findings" in prompt or "alpha_spec.json" in prompt and "Mission: turn" in prompt:
            roles.append("triage")
            (cwd / "alpha_spec.json").write_text(
                json.dumps(
                    {
                        "summary": "fix hitl",
                        "changes": [
                            {
                                "path": "src/autorefactor/cli.py",
                                "rationale": "f1",
                                "acceptance": "hitl down",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            return {"ok": True, "status": "finished", "duration_s": 0.2}
        roles.append("alpha")
        assert timeout_s == 0.05
        (cwd / "alpha_result.json").write_text(
            json.dumps({"ok": True, "notes": "partial"}), encoding="utf-8"
        )
        return {
            "ok": False,
            "status": "timeout",
            "timed_out": True,
            "duration_s": 0.05,
        }

    stim = {
        "episode_id": "ep1",
        "walks": [{"feedback_path": str(fb_path)}],
        "feedback_gate": {"product_use_ok": True},
        "critics_wall_s": 0.3,
        "engine": {"timing": {"analyze_s": 0.01}},
    }

    with (
        patch("ar_eval.loop.run_stimulus_critic", return_value=stim),
        patch("ar_eval.loop.run_local_agent", side_effect=fake_agent),
        patch(
            "ar_eval.loop.run_smoke_tests",
            return_value={"ok": True, "returncode": 0},
        ),
    ):
        out = _run_self_improve_loop_body(
            iterations=1,
            root=repo,
            eval_dir=eval_dir,
            engine_url="http://127.0.0.1:8765",
            model="composer-2.5",
            episodes=[episode],
            all_episodes=True,
            materialize_mode="copy",
            skip_agents=False,
            skip_rebuild=True,
            skip_retest=True,
            strict=True,
            require_engine=False,
            analyze=True,
            analyze_skip_llm=True,
            analyze_timeout=1.0,
            max_alpha_changes=3,
            progress=None,
            alpha_timeout=0.05,
            run_retest_agent=False,
        )

    assert out["ok"] is True
    assert "alpha" in roles
    iter_dir = eval_dir / "artifacts" / "loop" / "iter-001"
    assert (iter_dir / "smoke.json").is_file()
    alpha_run = json.loads((iter_dir / "alpha_run.json").read_text(encoding="utf-8"))
    assert alpha_run["status"] == "timeout"
    timing = json.loads((iter_dir / "timing.json").read_text(encoding="utf-8"))
    assert timing["agents"]["alpha_timed_out"] is True
    assert timing["agents"]["critics_wall_s"] == 0.3
