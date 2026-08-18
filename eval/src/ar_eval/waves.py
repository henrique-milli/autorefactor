"""CLI entry: ``ar-eval wave`` (subject teams) and ``ar-eval loop`` (self-improve)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from ar_eval.agents import mcp_autorefactor_stdio, run_local_agent, write_json
from ar_eval.corpus import load_corpus
from ar_eval.engine import reset_for_episode
from ar_eval.loop import run_self_improve_loop
from ar_eval.materialize import materialize_episode
from ar_eval.progress import LoopProgress, format_loop_summary
from ar_eval.rebuild import ensure_engine
from ar_eval.secrets import load_secret_files, resolve_cursor_api_key


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _eval_dir(repo_root: Path) -> Path:
    return repo_root / "eval"


def _read_persona(eval_dir: Path, name: str) -> str:
    path = eval_dir / "personas" / name
    if not path.is_file():
        raise FileNotFoundError(path)
    return path.read_text(encoding="utf-8")


def _critic_prompt(*, episode_id: str, persona: str) -> str:
    return (
        f"{persona}\n\n"
        f"Episode id: `{episode_id}`.\n"
        "The working directory is the materialized subject tree for this episode.\n"
        "Write `feedback.json` here when done.\n"
    )


def _maintainer_prompt(*, episode_id: str, persona: str) -> str:
    return (
        f"{persona}\n\n"
        f"Episode id: `{episode_id}`.\n"
        "Read `feedback.json` in the working directory and address findings.\n"
        "Use autorefactor MCP when available (remote engine already synced to this tree).\n"
    )


def run_episode(
    episode,
    *,
    repo_root: Path,
    eval_dir: Path,
    engine_url: str,
    model: str,
    restart_engine: bool,
    skip_agents: bool,
    materialize_mode: str,
) -> dict[str, Any]:
    subjects_dir = eval_dir / "subjects"
    artifacts_dir = eval_dir / "artifacts" / episode.id
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    workspace = materialize_episode(
        episode,
        repo_root=repo_root,
        subjects_dir=subjects_dir,
        mode=materialize_mode,
    )
    write_json(
        artifacts_dir / "materialize.json",
        {"episode_id": episode.id, "workspace": str(workspace), "roots": episode.roots},
    )

    reset_info = reset_for_episode(
        workspace,
        engine_url=engine_url,
        compose_dir=repo_root if restart_engine else None,
        restart=restart_engine,
        label=episode.id,
    )
    write_json(artifacts_dir / "engine_reset.json", reset_info)

    if skip_agents:
        return {
            "episode_id": episode.id,
            "workspace": str(workspace),
            "skipped_agents": True,
            "engine": reset_info,
        }

    critic_persona = _read_persona(eval_dir, "critic_dx.md")
    critic = run_local_agent(
        prompt=_critic_prompt(episode_id=episode.id, persona=critic_persona),
        cwd=workspace,
        model=model,
        mcp_servers=None,
    )
    write_json(artifacts_dir / "critic_run.json", critic)

    feedback_src = workspace / "feedback.json"
    if feedback_src.is_file():
        (artifacts_dir / "feedback.json").write_text(
            feedback_src.read_text(encoding="utf-8"), encoding="utf-8"
        )

    maint_persona = _read_persona(eval_dir, "maintainer.md")
    maintainer = run_local_agent(
        prompt=_maintainer_prompt(episode_id=episode.id, persona=maint_persona),
        cwd=workspace,
        model=model,
        mcp_servers=mcp_autorefactor_stdio(
            remote_url=engine_url, workspace=workspace
        ),
    )
    write_json(artifacts_dir / "maintainer_run.json", maintainer)

    result_src = workspace / "maintainer_result.json"
    if result_src.is_file():
        (artifacts_dir / "maintainer_result.json").write_text(
            result_src.read_text(encoding="utf-8"), encoding="utf-8"
        )

    summary = {
        "episode_id": episode.id,
        "workspace": str(workspace),
        "critic": critic,
        "maintainer": maintainer,
        "engine": reset_info,
    }
    write_json(artifacts_dir / "summary.json", summary)
    return summary


def run_waves(
    *,
    repo_root: Path | None = None,
    engine_url: str = "http://127.0.0.1:8765",
    model: str = "composer-2.5",
    max_episodes: int | None = None,
    episode_ids: list[str] | None = None,
    restart_engine: bool = False,
    skip_agents: bool = False,
    use_local_corpus: bool = True,
    materialize_mode: str = "copy",
    require_engine: bool = True,
) -> list[dict[str, Any]]:
    root = repo_root or _repo_root()
    eval_dir = _eval_dir(root)
    load_secret_files(eval_dir / "secrets.local.env", root / ".env", override=False)
    if not skip_agents and not resolve_cursor_api_key(repo_root=root, eval_dir=eval_dir):
        raise RuntimeError(
            "CURSOR_API_KEY missing. Add to eval/secrets.local.env:\n"
            "  CURSOR_API_KEY=cursor_...\n"
            "Or put the same line in the repo .env (gitignored)."
        )

    corpus = load_corpus(eval_dir=eval_dir, use_local=use_local_corpus)

    if require_engine:
        ensure_engine(root, engine_url=engine_url)

    episodes = list(corpus.episodes)
    if episode_ids:
        want = set(episode_ids)
        episodes = [e for e in episodes if e.id in want]
    if max_episodes is not None:
        episodes = episodes[: max(0, max_episodes)]

    results: list[dict[str, Any]] = []
    for ep in episodes:
        results.append(
            run_episode(
                ep,
                repo_root=root,
                eval_dir=eval_dir,
                engine_url=engine_url,
                model=model,
                restart_engine=restart_engine,
                skip_agents=skip_agents,
                materialize_mode=materialize_mode,
            )
        )
    return results


def _add_shared_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--engine-url",
        default="http://127.0.0.1:8765",
        help="Autorefactor engine base URL",
    )
    p.add_argument("--model", default="composer-2.5", help="Cursor SDK model id")
    p.add_argument(
        "--episode",
        action="append",
        dest="episodes",
        default=None,
        help="Only run episode id (repeatable)",
    )
    p.add_argument(
        "--example-only",
        action="store_true",
        help="Ignore corpus.local.yaml even if present",
    )
    p.add_argument(
        "--symlink",
        action="store_true",
        help="Symlink roots into subjects/ instead of copy",
    )
    p.add_argument(
        "--skip-agents",
        action="store_true",
        help="Plumbing only (no Cursor SDK)",
    )
    p.add_argument(
        "--no-engine",
        action="store_true",
        help="Do not require engine health",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ar-eval",
        description="Agent-teams eval and self-improve loop.",
    )
    sub = parser.add_subparsers(dest="command")

    wave = sub.add_parser("wave", help="Critic → subject maintainer (one corpus pass)")
    _add_shared_flags(wave)
    wave.add_argument("--max-episodes", type=int, default=None)
    wave.add_argument(
        "--restart-engine",
        action="store_true",
        help="docker compose restart between episodes (no rebuild)",
    )

    loop = sub.add_parser(
        "loop",
        help="Self-improve: all personas × all episodes → spec-probe retest (N times)",
    )
    _add_shared_flags(loop)
    loop.add_argument(
        "--iterations",
        type=int,
        default=3,
        help="Number of self-improve iterations (default 3)",
    )
    loop.add_argument(
        "--all-episodes",
        action="store_true",
        help="Run full corpus each iteration (default). Kept for compatibility.",
    )
    loop.add_argument(
        "--rotate-pairs",
        action="store_true",
        help="Cheap debug: one persona × one episode per iteration (old pairing)",
    )
    loop.add_argument(
        "--strict",
        action="store_true",
        help="Abort after smoke repair still fails, or any episode spec-probe retest fails",
    )
    loop.add_argument(
        "--skip-rebuild",
        action="store_true",
        help="Skip compose restart after alpha (dev). Retest still runs if alpha applied.",
    )
    loop.add_argument(
        "--skip-retest",
        action="store_true",
        help="Skip analyze+probe retest (dev). Default: spec probes on every episode (no Cursor agent).",
    )
    loop.add_argument(
        "--retest-agent",
        action="store_true",
        help="Also run the opt-in Cursor scorer on prior finding ids after probe",
    )
    loop.add_argument(
        "--no-analyze",
        action="store_true",
        help="Skip engine analyze after sync (no UI run)",
    )
    loop.add_argument(
        "--analyze-skip-llm",
        action="store_true",
        help="Faster analyze without LLM describe",
    )
    loop.add_argument(
        "--analyze-timeout",
        type=float,
        default=3600.0,
        help="Max seconds to wait for analyze job",
    )
    loop.add_argument(
        "--max-alpha-changes",
        type=int,
        default=3,
        help="Max product changes per iteration (triage budget)",
    )
    loop.add_argument(
        "--alpha-timeout",
        type=float,
        default=300.0,
        help="Fail-soft seconds for alpha / smoke-repair agents (default 300)",
    )
    loop.add_argument(
        "--no-progress",
        action="store_true",
        help="Timestamped lines instead of a live dashboard",
    )
    loop.add_argument(
        "--json",
        action="store_true",
        help="Print full summary JSON after the short table",
    )

    # Backward compatible: flags without subcommand ⇒ wave
    _add_shared_flags(parser)
    parser.add_argument("--max-episodes", type=int, default=None)
    parser.add_argument("--restart-engine", action="store_true")
    return parser


def _run_wave_from_args(args: argparse.Namespace) -> int:
    results = run_waves(
        engine_url=args.engine_url,
        model=args.model,
        max_episodes=getattr(args, "max_episodes", None),
        episode_ids=args.episodes,
        restart_engine=bool(getattr(args, "restart_engine", False)),
        skip_agents=args.skip_agents,
        use_local_corpus=not args.example_only,
        materialize_mode="symlink" if args.symlink else "copy",
        require_engine=not args.no_engine,
    )
    print(json.dumps({"ok": True, "episodes": len(results), "results": results}, indent=2))
    for r in results:
        if r.get("skipped_agents"):
            continue
        for role in ("critic", "maintainer"):
            block = r.get(role) or {}
            if block and not block.get("ok", True):
                return 2
    return 0


def _run_loop_from_args(args: argparse.Namespace) -> int:
    progress = LoopProgress(force_lines=bool(args.no_progress))
    summary = run_self_improve_loop(
        iterations=args.iterations,
        engine_url=args.engine_url,
        model=args.model,
        episode_ids=args.episodes,
        use_local_corpus=not args.example_only,
        all_episodes=bool(getattr(args, "all_episodes", False))
        or not bool(getattr(args, "rotate_pairs", False)),
        materialize_mode="symlink" if args.symlink else "copy",
        skip_agents=args.skip_agents,
        skip_rebuild=args.skip_rebuild,
        skip_retest=args.skip_retest,
        strict=args.strict,
        require_engine=not args.no_engine,
        analyze=not args.no_analyze,
        analyze_skip_llm=args.analyze_skip_llm,
        analyze_timeout=args.analyze_timeout,
        max_alpha_changes=args.max_alpha_changes,
        alpha_timeout=args.alpha_timeout,
        run_retest_agent=bool(getattr(args, "retest_agent", False)),
        progress=progress,
    )
    artifacts = _eval_dir(_repo_root()) / "artifacts" / "loop" / "summary.json"
    print(
        format_loop_summary(summary, artifacts=str(artifacts)),
        end="",
    )
    if args.json:
        print(json.dumps(summary, indent=2, default=str))
    if args.strict:
        for it in summary.get("results") or []:
            smoke = it.get("smoke") or {}
            if smoke and not smoke.get("ok", True):
                return 2
            retest = it.get("retest") or {}
            if retest and retest.get("ok") and not retest.get("passed"):
                return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "loop":
            return _run_loop_from_args(args)
        # "wave" or legacy no-subcommand
        return _run_wave_from_args(args)
    except Exception as exc:  # noqa: BLE001
        print(f"ar-eval failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
