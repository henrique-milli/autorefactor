"""Self-improve loop: product-user lifecycle → triage → alpha → rebuild → retest."""

from __future__ import annotations

import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from ar_eval.agents import mcp_autorefactor_stdio, run_local_agent, write_json
from ar_eval.allowlist import filter_allowed_changes, rank_and_budget_changes
from ar_eval.corpus import Episode, load_corpus
from ar_eval.engine import analyze_run_slug, fetch_changesets_for_slug, reset_for_episode
from ar_eval.engine_lock import EngineLock
from ar_eval.evidence import verify_lifecycle_evidence, write_snapshot
from ar_eval.materialize import materialize_episode
from ar_eval.probe import (
    apply_spec_probes,
    probe_queue_changesets,
    spec_expect_coverage_prep,
    spec_targets_planner,
    sticky_changeset_ids,
)
from ar_eval.progress import LoopProgress, format_analyze_job, format_retest_cell
from ar_eval.rebuild import ensure_engine, rebuild_engine
from ar_eval.retest import load_prior_findings, retest_prompt, score_retest
from ar_eval.secrets import load_secret_files, resolve_cursor_api_key
from ar_eval.timing import record_duration

DEFAULT_SMOKE_TESTS = (
    "tests/test_ar_eval_corpus.py",
    "tests/test_ar_eval_loop.py",
    "tests/test_runtime_backends.py",
    "tests/test_jobs.py",
    "tests/test_paths.py",
    "tests/test_report_stale_gate.py",
    "tests/test_roi_queue_hitl.py",
    "tests/test_sample_queue.py",
)

USER_PERSONAS = (
    "user_product_critic.md",
    "user_coverage_purist.md",
    "user_confidence_skeptic.md",
    "user_semantics_maximalist.md",
)

BASE_USER_PERSONA = "user_product_critic.md"

MAX_ALPHA_CHANGES = 3
RETEST_PASS_SCORE = 0.6
SMOKE_REPAIR_ATTEMPTS = 1
HITL_SAMPLE_MIN = 1
HITL_SAMPLE_MAX = 3
DEFAULT_ALPHA_TIMEOUT_S = 300.0


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _eval_dir(repo_root: Path) -> Path:
    return repo_root / "eval"


def _read_persona(eval_dir: Path, name: str) -> str:
    path = eval_dir / "personas" / name
    if not path.is_file():
        raise FileNotFoundError(path)
    return path.read_text(encoding="utf-8")


def load_user_persona(eval_dir: Path, name: str) -> str:
    """
    Bias personas are short overlays; always prepend the sample-walk base
    so agents never get a prompt that only says "same as user_product_critic".
    """
    base = _read_persona(eval_dir, BASE_USER_PERSONA)
    if name == BASE_USER_PERSONA:
        return base
    bias = _read_persona(eval_dir, name)
    return (
        f"{base}\n\n---\n\n## Persona bias for this run\n\n{bias.strip()}\n"
    )


def select_persona(iteration: int) -> str:
    return USER_PERSONAS[(iteration - 1) % len(USER_PERSONAS)]


def _persona_stem(name: str) -> str:
    stem = name.rsplit("/", 1)[-1]
    if stem.endswith(".md"):
        stem = stem[:-3]
    return stem.removeprefix("user_")


def select_episodes_for_iteration(
    episodes: list[Episode],
    *,
    iteration: int,
    all_episodes: bool,
    sticky_episode_id: str | None = None,
) -> list[Episode]:
    """
    Pick stimulus episodes for iteration ``iteration`` (1-based).

    Default (``all_episodes=True``): every corpus episode. A sticky id (failed
    retest) is moved to the front, not used as the exclusive episode.
    ``all_episodes=False`` (``--rotate-pairs``): one episode, cycling
    ``episodes[(iteration-1) % len]``, sticky exclusive if set.
    """
    if not episodes:
        return []
    if all_episodes:
        ordered = list(episodes)
        if sticky_episode_id:
            sticky = [e for e in ordered if e.id == sticky_episode_id]
            rest = [e for e in ordered if e.id != sticky_episode_id]
            return sticky + rest
        return ordered
    if sticky_episode_id:
        for ep in episodes:
            if ep.id == sticky_episode_id:
                return [ep]
    return [episodes[(iteration - 1) % len(episodes)]]


def _progress_analyze_cb(progress: LoopProgress | None):
    if progress is None:
        return None

    def _cb(job: dict[str, Any]) -> None:
        progress.heartbeat(format_analyze_job(job))

    return _cb


def _progress_heartbeat(progress: LoopProgress | None):
    if progress is None:
        return None
    return progress.heartbeat


def _iteration_row(payload: dict[str, Any]) -> dict[str, Any]:
    gates = [
        bool((s.get("feedback_gate") or {}).get("product_use_ok"))
        for s in (payload.get("stimulus") or [])
        if not s.get("skipped_agents")
    ]
    if payload.get("skipped_agents"):
        user = "skip"
    elif gates and any(gates):
        user = "ok"
    else:
        user = "fail"
    smoke = payload.get("smoke") or {}
    smoke_s = "—" if not smoke else ("pass" if smoke.get("ok") else "fail")
    retest_s = format_retest_cell(payload)
    return {
        "iteration": payload.get("iteration"),
        "user": user,
        "smoke": smoke_s,
        "retest": retest_s,
        "sticky": payload.get("sticky_next_episode"),
    }


def _iteration_timing_payload(
    *,
    iteration: int,
    stim_results: list[dict[str, Any]],
    retest_results: list[dict[str, Any]] | None = None,
    rebuild_info: dict[str, Any] | None,
    ensure_info: dict[str, Any] | None,
    agents: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the per-iteration timing JSON schema (written to `timing.json`)."""
    episodes: dict[str, Any] = {}

    for stim in stim_results:
        ep_id = str(stim.get("episode_id") or "")
        if not ep_id:
            continue
        engine = stim.get("engine")
        timing_blob = engine.get("timing") if isinstance(engine, dict) else None
        stimulus: dict[str, Any] = {}
        if timing_blob:
            stimulus["timing"] = timing_blob
        walks_out: list[dict[str, Any]] = []
        for walk in stim.get("walks") or []:
            if not isinstance(walk, dict):
                continue
            walks_out.append(
                {
                    "persona": walk.get("persona_stem") or walk.get("persona"),
                    "critic_s": walk.get("critic_s"),
                }
            )
        if walks_out:
            stimulus["walks"] = walks_out
        if stim.get("critics_wall_s") is not None:
            stimulus["critics_wall_s"] = stim.get("critics_wall_s")
        if stimulus:
            episodes.setdefault(ep_id, {})["stimulus"] = stimulus

    for one in retest_results or []:
        if not isinstance(one, dict):
            continue
        ep_id = str(one.get("episode_id") or "")
        if not ep_id:
            continue
        engine = one.get("engine")
        timing_blob = engine.get("timing") if isinstance(engine, dict) else None
        if timing_blob:
            episodes.setdefault(ep_id, {})["retest"] = {"timing": timing_blob}
        probe = one.get("probe")
        if isinstance(probe, dict) and probe.get("timing"):
            episodes.setdefault(ep_id, {}).setdefault("retest", {})["probe_timing"] = (
                probe.get("timing")
            )

    return {
        "iteration": iteration,
        "ensure_engine": (ensure_info or {}).get("timing"),
        "rebuild": (rebuild_info or {}).get("timing"),
        "episodes": episodes,
        "agents": agents or {},
    }


def _agents_timing(
    *,
    stim_results: list[dict[str, Any]],
    triage: dict[str, Any] | None = None,
    alpha: dict[str, Any] | None = None,
    retest_info: dict[str, Any] | None = None,
) -> dict[str, Any]:
    walls = [
        float(s["critics_wall_s"])
        for s in stim_results
        if isinstance(s, dict) and s.get("critics_wall_s") is not None
    ]
    out: dict[str, Any] = {
        "alpha_timed_out": bool(
            alpha
            and (
                alpha.get("timed_out")
                or str(alpha.get("status") or "").lower() == "timeout"
            )
        ),
    }
    if walls:
        out["critics_wall_s"] = sum(walls)
    if triage and triage.get("duration_s") is not None:
        out["triage_s"] = triage["duration_s"]
    if alpha and alpha.get("duration_s") is not None:
        out["alpha_s"] = alpha["duration_s"]
    agent = (retest_info or {}).get("agent") if retest_info else None
    if isinstance(agent, dict) and agent.get("duration_s") is not None:
        out["retest_agent_s"] = agent["duration_s"]
    return out


def _ensure_duration(result: dict[str, Any], measured_s: float) -> dict[str, Any]:
    if result.get("duration_s") is None:
        result["duration_s"] = measured_s
    return result


def _write_iteration_timing_json(iter_dir: Path, payload: dict[str, Any]) -> None:
    write_json(iter_dir / "timing.json", payload)


def _copy_if_exists(src: Path, dest: Path) -> bool:
    if not src.is_file():
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    return True


def run_smoke_tests(
    repo_root: Path,
    *,
    tests: tuple[str, ...] = DEFAULT_SMOKE_TESTS,
) -> dict[str, Any]:
    cmd = ["uv", "run", "pytest", "-q", "--tb=line", *tests]
    proc = subprocess.run(cmd, cwd=str(repo_root), capture_output=True, text=True)
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "cmd": cmd,
        "stdout_tail": (proc.stdout or "")[-2000:],
        "stderr_tail": (proc.stderr or "")[-2000:],
    }


def _critic_prompt(
    *,
    episode_id: str,
    persona: str,
    engine_url: str,
    feedback_path: Path | None = None,
    prior_findings: list[dict[str, Any]] | None = None,
    prior_spec: dict[str, Any] | None = None,
    last_retest: dict[str, Any] | None = None,
) -> str:
    out_line = ""
    if feedback_path is not None:
        out_line = (
            f"Write `feedback.json` to this exact path: `{feedback_path.resolve()}`.\n"
        )
    memory = ""
    if prior_findings:
        titles = []
        for finding in prior_findings[:12]:
            if not isinstance(finding, dict):
                continue
            fid = finding.get("local_id") or finding.get("id")
            titles.append(f"- `{fid}` {finding.get('title') or ''} ({finding.get('kind') or ''})")
        memory += (
            "Prior findings from your last walk on this episode (do not re-essay "
            "resolved items without new MCP evidence; mark still_open / resolved / new):\n"
            + "\n".join(titles)
            + "\n"
        )
    if prior_spec and (prior_spec.get("changes") or []):
        paths = [
            str(c.get("path"))
            for c in prior_spec.get("changes") or []
            if isinstance(c, dict) and c.get("path")
        ]
        memory += (
            "Product patches since that walk (acceptance to verify): "
            + "; ".join(paths)
            + "\n"
        )
    if last_retest:
        n = last_retest.get("ensure_tests_count")
        memory += (
            f"Harness last retest for this episode: ensure_tests_count={n} "
            f"(probe_ok={last_retest.get('ok')}).\n"
        )
        if isinstance(n, (int, float)) and float(n) >= 1:
            memory += (
                "If ensure_tests_count>=1, do not mark zero/missing ensure_tests prep "
                "as still_open unless this walk's get_report / sample_queue shows count 0. "
                "Judge quality of prep/HITL, not existence the probe already measured.\n"
            )
    return (
        f"{persona}\n\n"
        f"Episode id: `{episode_id}`.\n"
        f"Engine URL (MCP already configured): `{engine_url}`.\n"
        "Analyze has already completed for this synced workspace. "
        "Trust get_status / get_report unless they show the run is not ready.\n"
        "Mission: evaluate this analyzed product via MCP (status, report, sample_queue "
        f"with 1 autorun brief + {HITL_SAMPLE_MIN}–{HITL_SAMPLE_MAX} HITL briefs). "
        "Success is product-surface evidence in feedback.json, not a subject rebuild.\n"
        + memory
        + out_line
        + "File must have `source=product_use`, `hitl_sampled` / `queue_sampled_ids`, "
        "and `mcp_tools_used`.\n"
    )


def _triage_prompt(
    *,
    feedback_path: Path,
    persona: str,
    iteration: int,
    product_use_ok: bool,
) -> str:
    gate = (
        "Feedback gate: OK (hard evidence passed)."
        if product_use_ok
        else (
            "Feedback gate: FAILED — hard evidence missing. "
            "Write alpha_spec.json with changes=[] and "
            "stop_reason=feedback_not_from_product_use."
        )
    )
    return (
        f"{persona}\n\n"
        f"Self-improve iteration: {iteration}.\n"
        f"{gate}\n"
        "Feedback is merged from multiple personas and episodes; findings may include "
        "`persona` and `episode_id` tags. Deduplicate overlapping complaints.\n"
        f"Critic feedback file: `{feedback_path}`.\n"
        "Mission: turn sample-walk findings into at most "
        f"{MAX_ALPHA_CHANGES} allowlisted product changes, highest-severity first.\n"
        "Success is `alpha_spec.json` at the repository root. "
        "Out of scope: subject-repo work and extra features beyond the findings.\n"
    )


def _alpha_prompt(*, persona: str, iteration: int) -> str:
    return (
        f"{persona}\n\n"
        f"Self-improve iteration: {iteration}.\n"
        "Read `alpha_spec.json` at the repository root. Mission: meet each change "
        "acceptance with a small allowlisted diff, then stop. "
        "Write `alpha_result.json` when done.\n"
    )


def _smoke_fix_prompt(*, persona: str, iteration: int, smoke: dict[str, Any]) -> str:
    cmd = smoke.get("cmd") or []
    cmd_s = " ".join(str(c) for c in cmd) if isinstance(cmd, list) else str(cmd)
    stdout = str(smoke.get("stdout_tail") or "").strip() or "(empty)"
    stderr = str(smoke.get("stderr_tail") or "").strip() or "(empty)"
    return (
        f"{persona}\n\n"
        f"Self-improve iteration: {iteration}.\n"
        "Smoke tests failed after the last product patch. "
        "Mission: restore green harness smoke from the traceback. "
        "Out of scope: new features and a product rebuild.\n"
        "Only edit allowlisted paths. Prefer the smallest revert/fix that restores green smoke.\n"
        f"Command: `{cmd_s}`\n"
        f"Return code: {smoke.get('returncode')}\n\n"
        f"stdout (tail):\n```\n{stdout[-4000:]}\n```\n\n"
        f"stderr (tail):\n```\n{stderr[-4000:]}\n```\n\n"
        "Write `alpha_result.json` when the fix is in place "
        '(include `"notes": "smoke_repair"`).\n'
    )


def repair_failed_smoke(
    *,
    repo_root: Path,
    eval_dir: Path,
    iter_dir: Path,
    iteration: int,
    model: str,
    smoke: dict[str, Any],
    attempts: int = SMOKE_REPAIR_ATTEMPTS,
    run_agent=None,
    run_tests=None,
    on_heartbeat=None,
    timeout_s: float | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """
    If smoke failed, send pytest output to the alpha agent and re-run tests.

    Returns ``(latest_smoke, repair_runs)``. Does not rebuild.
    """
    agent_fn = run_agent or run_local_agent
    tests_fn = run_tests or run_smoke_tests
    repairs: list[dict[str, Any]] = []
    current = smoke
    for n in range(1, max(0, attempts) + 1):
        if current.get("ok"):
            break
        repair = agent_fn(
            prompt=_smoke_fix_prompt(
                persona=_read_persona(eval_dir, "alpha_improver.md"),
                iteration=iteration,
                smoke=current,
            ),
            cwd=repo_root,
            model=model,
            on_heartbeat=on_heartbeat,
            timeout_s=timeout_s,
        )
        repairs.append(repair)
        write_json(iter_dir / f"smoke_repair_{n:02d}.json", repair)
        current = tests_fn(repo_root)
        write_json(iter_dir / "smoke.json", current)
    return current, repairs


def _analyze_succeeded(reset_info: dict[str, Any]) -> bool:
    analyze = reset_info.get("analyze")
    if not analyze:
        return False
    job = analyze.get("job") if isinstance(analyze, dict) else None
    if isinstance(job, dict):
        status = str(job.get("status") or "").lower()
        if status in {"failed", "cancelled", "error"}:
            return False
        if status in {"done", "completed", "success", "finished"}:
            return True
    started = analyze.get("started") if isinstance(analyze, dict) else None
    if isinstance(started, dict) and started.get("ok") is False:
        return False
    if isinstance(analyze, dict) and analyze.get("ok") is True and not analyze.get("job_id"):
        return True
    status = str(analyze.get("status") or "").lower() if isinstance(analyze, dict) else ""
    return status in {"done", "completed", "success", "finished"}


def feedback_looks_like_product_use(data: dict[str, Any]) -> bool:
    """Soft heuristic (tests); production gate uses ``verify_lifecycle_evidence``."""
    if str(data.get("source") or "").lower() == "subject_review":
        return False
    lifecycle = data.get("lifecycle") if isinstance(data.get("lifecycle"), dict) else {}
    if lifecycle.get("report_reviewed") and (
        int(lifecycle.get("auto_proceed_applied") or 0)
        + int(lifecycle.get("hitl_proceeded") or 0)
        + int(lifecycle.get("hitl_sampled") or 0)
        > 0
    ):
        return True
    if str(data.get("source") or "").lower() != "product_use":
        return False
    findings = data.get("findings") or []
    if not isinstance(findings, list):
        return False
    for f in findings:
        if not isinstance(f, dict):
            continue
        if f.get("product_surface") or f.get("kind"):
            return True
        for ev in f.get("evidence") or []:
            if isinstance(ev, dict) and (ev.get("changeset_id") or ev.get("sensor")):
                return True
    return False


def _finding_meta_from_feedback(data: dict[str, Any]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for f in data.get("findings") or []:
        if not isinstance(f, dict) or not f.get("id"):
            continue
        info = {
            "severity": f.get("severity"),
            "kind": f.get("kind"),
        }
        fid = str(f["id"]).lower()
        out[fid] = info
        local = str(f.get("local_id") or "").lower()
        if local:
            out[local] = info
        if ":" in fid:
            out[fid.split(":")[-1]] = info
    return out


def _sanitize_alpha_spec(
    repo_root: Path,
    *,
    finding_meta: dict[str, dict] | None = None,
    max_changes: int = MAX_ALPHA_CHANGES,
) -> dict[str, Any]:
    path = repo_root / "alpha_spec.json"
    if not path.is_file():
        return {"summary": "", "changes": [], "stop_reason": "missing_alpha_spec"}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"summary": "", "changes": [], "stop_reason": "invalid_json"}
    if not isinstance(data, dict):
        return {"summary": "", "changes": [], "stop_reason": "not_object"}
    changes = data.get("changes") or []
    if not isinstance(changes, list):
        changes = []
    kept, rejected = filter_allowed_changes(changes)
    budgeted = rank_and_budget_changes(
        kept, max_changes=max_changes, finding_meta=finding_meta
    )
    data["changes"] = budgeted
    if rejected:
        data["rejected_paths"] = rejected
    if len(kept) > len(budgeted):
        data["budget_dropped"] = len(kept) - len(budgeted)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return data


def _gate_feedback_file(
    fb_path: Path,
    *,
    workspace: Path,
    dest_dir: Path,
) -> dict[str, Any]:
    feedback_meta: dict[str, Any] = {"product_use_ok": False}
    if fb_path.is_file():
        try:
            fb = json.loads(fb_path.read_text(encoding="utf-8"))
            if isinstance(fb, dict):
                evidence = verify_lifecycle_evidence(fb, workspace=workspace)
                write_json(dest_dir / "evidence.json", evidence)
                feedback_meta["product_use_ok"] = bool(evidence.get("ok"))
                feedback_meta["source"] = fb.get("source")
                feedback_meta["evidence_reasons"] = evidence.get("reasons")
                if not feedback_meta["product_use_ok"]:
                    feedback_meta["warning"] = (
                        "hard evidence gate failed; triage should no-op"
                    )
        except json.JSONDecodeError:
            feedback_meta["warning"] = "feedback.json is not valid JSON"
    else:
        feedback_meta["warning"] = "missing feedback.json"
    write_json(dest_dir / "feedback_gate.json", feedback_meta)
    return feedback_meta


def _merge_feedback_blobs(
    blobs: list[dict[str, Any]],
    *,
    iteration: int,
    product_use_ok: bool,
) -> dict[str, Any]:
    findings: list[Any] = []
    walks: list[dict[str, Any]] = []
    for blob in blobs:
        persona = str(blob.get("persona") or "unknown")
        eid = blob.get("episode_id")
        raw_findings = blob.get("findings") or []
        walks.append(
            {
                "persona": persona,
                "episode_id": eid,
                "finding_count": len(raw_findings) if isinstance(raw_findings, list) else 0,
            }
        )
        if not isinstance(raw_findings, list):
            continue
        for finding in raw_findings:
            if not isinstance(finding, dict):
                continue
            tagged = dict(finding)
            tagged.setdefault("persona", persona)
            if eid:
                tagged.setdefault("episode_id", eid)
            local_id = str(tagged.get("id") or "f")
            tagged["local_id"] = local_id
            if eid:
                tagged["id"] = f"{eid}:{persona}:{local_id}"
            findings.append(tagged)
    return {
        "episode_id": f"iter-{iteration}",
        "source": "product_use" if product_use_ok else "subject_review",
        "summary": "merged multi-persona stimulus feedback",
        "walks": walks,
        "findings": findings,
    }


def _load_feedback_blob(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        blob = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return blob if isinstance(blob, dict) else None


def _load_last_retest_probe(prev: Path, episode_id: str) -> dict[str, Any] | None:
    """Previous iteration's per-episode retest probe facts for critic memory."""
    blob = _load_feedback_blob(prev / episode_id / "retest_probe.json")
    if isinstance(blob, dict) and (
        blob.get("ensure_tests_count") is not None
        or blob.get("failures") is not None
        or blob.get("ok") is not None
    ):
        return {
            "ensure_tests_count": blob.get("ensure_tests_count"),
            "ok": blob.get("ok"),
            "failures": list(blob.get("failures") or []),
            "notes": list(blob.get("notes") or []),
        }
    summary = _load_feedback_blob(prev / "retest_summary.json")
    episodes = summary.get("episodes") if isinstance(summary, dict) else None
    if not isinstance(episodes, list):
        return None
    for ep in episodes:
        if not isinstance(ep, dict):
            continue
        if str(ep.get("episode_id") or "") != episode_id:
            continue
        probe = ep.get("probe") if isinstance(ep.get("probe"), dict) else ep
        return {
            "ensure_tests_count": probe.get("ensure_tests_count"),
            "ok": probe.get("ok") if "ok" in probe else ep.get("passed"),
            "failures": list(probe.get("failures") or []),
            "notes": list(probe.get("notes") or []),
        }
    return None


def _load_prior_critic_memory(
    *,
    loop_root: Path,
    iteration: int,
    episode_id: str,
    persona_files: list[str],
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any] | None, dict[str, Any] | None]:
    """Prior walk findings per persona stem, last sanitized alpha_spec, last retest probe."""
    if iteration <= 1:
        return {}, None, None
    prev = loop_root / f"iter-{iteration - 1:03d}"
    spec: dict[str, Any] | None = None
    spec_path = prev / "alpha_spec.sanitized.json"
    if spec_path.is_file():
        try:
            loaded = json.loads(spec_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                spec = loaded
        except json.JSONDecodeError:
            spec = None
    by_persona: dict[str, list[dict[str, Any]]] = {}
    for pf in persona_files:
        stem = _persona_stem(pf)
        blob = _load_feedback_blob(prev / episode_id / stem / "feedback.json")
        findings = blob.get("findings") if blob else None
        if isinstance(findings, list):
            by_persona[stem] = [f for f in findings if isinstance(f, dict)]
    return by_persona, spec, _load_last_retest_probe(prev, episode_id)


def _run_one_persona_walk(
    *,
    episode: Episode,
    eval_dir: Path,
    art_dir: Path,
    workspace: Path,
    engine_url: str,
    model: str,
    persona_file: str,
    progress: LoopProgress | None,
    prior_findings: list[dict[str, Any]] | None = None,
    prior_spec: dict[str, Any] | None = None,
    last_retest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    stem = _persona_stem(persona_file)
    pdir = art_dir / stem
    pdir.mkdir(parents=True, exist_ok=True)
    fb_path = pdir / "feedback.json"
    if prior_findings or prior_spec or last_retest:
        write_json(
            pdir / "prior_feedback.json",
            {
                "findings": prior_findings or [],
                "alpha_spec": prior_spec or {},
                "last_retest": last_retest or {},
            },
        )
    timing: dict[str, Any] = {}
    with record_duration("critic_s", timing):
        critic = run_local_agent(
            prompt=_critic_prompt(
                episode_id=episode.id,
                persona=load_user_persona(eval_dir, persona_file),
                engine_url=engine_url,
                feedback_path=fb_path,
                prior_findings=prior_findings,
                prior_spec=prior_spec,
                last_retest=last_retest,
            ),
            cwd=workspace,
            model=model,
            mcp_servers=mcp_autorefactor_stdio(
                remote_url=engine_url, workspace=workspace
            ),
            on_heartbeat=_progress_heartbeat(progress),
        )
    _ensure_duration(critic, float(timing.get("critic_s") or 0.0))
    write_json(pdir / "critic_run.json", critic)
    if not fb_path.is_file():
        _copy_if_exists(workspace / "feedback.json", fb_path)
    gate = _gate_feedback_file(fb_path, workspace=workspace, dest_dir=pdir)
    return {
        "episode_id": episode.id,
        "persona": persona_file,
        "persona_stem": stem,
        "critic": critic,
        "critic_s": critic.get("duration_s"),
        "feedback_path": str(fb_path),
        "feedback_gate": gate,
    }


def run_stimulus_critic(
    episode: Episode,
    *,
    repo_root: Path,
    eval_dir: Path,
    art_dir: Path,
    engine_url: str,
    model: str,
    materialize_mode: str,
    skip_agents: bool,
    analyze: bool = True,
    analyze_skip_llm: bool = False,
    analyze_timeout: float = 3600.0,
    persona_file: str | None = None,
    persona_files: list[str] | None = None,
    progress: LoopProgress | None = None,
    prior_by_persona: dict[str, list[dict[str, Any]]] | None = None,
    prior_spec: dict[str, Any] | None = None,
    last_retest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    files = list(persona_files) if persona_files else [persona_file or BASE_USER_PERSONA]
    mode = "copy" if not skip_agents else materialize_mode
    if progress:
        progress.set_context(
            episode=episode.id,
            persona=files[0] if len(files) == 1 else f"{len(files)} personas",
        )
        progress.phase("copy", f"materialize {episode.id}")
    workspace = materialize_episode(
        episode,
        repo_root=repo_root,
        subjects_dir=eval_dir / "subjects",
        mode=mode,
    )
    write_snapshot(workspace)

    lock_path = eval_dir / "artifacts" / ".engine.lock"
    if progress:
        progress.phase("restart", "compose restart + sync + analyze")
    with EngineLock(lock_path):
        reset_info = reset_for_episode(
            workspace,
            engine_url=engine_url,
            compose_dir=repo_root,
            restart=True,
            label=f"loop-{episode.id}",
            analyze=analyze,
            analyze_skip_llm=analyze_skip_llm,
            analyze_timeout=analyze_timeout,
            on_progress=_progress_analyze_cb(progress),
        )
    write_json(art_dir / "engine_reset.json", reset_info)
    write_json(
        art_dir / "materialize.json",
        {
            "episode_id": episode.id,
            "workspace": str(workspace),
            "mode": mode,
            "analyze": analyze,
            "personas": files,
        },
    )

    if skip_agents:
        return {
            "episode_id": episode.id,
            "workspace": str(workspace),
            "skipped_agents": True,
            "engine": reset_info,
        }

    if not analyze:
        raise RuntimeError(
            "self-improve loop refuses product-user agents without analyze. "
            "Omit --no-analyze."
        )
    if not _analyze_succeeded(reset_info):
        raise RuntimeError(
            "analyze did not succeed; refusing critic step. "
            f"reset_info.analyze={reset_info.get('analyze')!r}"
        )

    if progress:
        progress.phase("user", f"{len(files)} personas sample walk")

    def _walk(pf: str) -> dict[str, Any]:
        stem = _persona_stem(pf)
        return _run_one_persona_walk(
            episode=episode,
            eval_dir=eval_dir,
            art_dir=art_dir,
            workspace=workspace,
            engine_url=engine_url,
            model=model,
            persona_file=pf,
            progress=progress,
            prior_findings=(prior_by_persona or {}).get(stem) or [],
            prior_spec=prior_spec,
            last_retest=last_retest,
        )

    wall: dict[str, Any] = {}
    with record_duration("critics_wall_s", wall):
        if len(files) == 1:
            walks = [_walk(files[0])]
        else:
            with ThreadPoolExecutor(max_workers=len(files)) as pool:
                walks = list(pool.map(_walk, files))

    blobs: list[dict[str, Any]] = []
    for walk in walks:
        blob = _load_feedback_blob(Path(walk["feedback_path"]))
        if blob is not None:
            blob.setdefault("persona", _persona_stem(str(walk.get("persona") or "")))
            blob.setdefault("episode_id", episode.id)
            blobs.append(blob)

    gates = [bool((w.get("feedback_gate") or {}).get("product_use_ok")) for w in walks]
    episode_ok = any(gates)
    merged = _merge_feedback_blobs(blobs, iteration=0, product_use_ok=episode_ok)
    merged["episode_id"] = episode.id
    write_json(art_dir / "feedback.json", merged)
    write_json(
        art_dir / "feedback_gate.json",
        {"product_use_ok": episode_ok, "walks": len(walks)},
    )

    return {
        "episode_id": episode.id,
        "workspace": str(workspace),
        "engine": reset_info,
        "walks": walks,
        "critics_wall_s": wall.get("critics_wall_s"),
        "feedback_path": str(art_dir / "feedback.json"),
        "feedback_gate": {"product_use_ok": episode_ok},
        "personas": files,
    }


def run_retest_episode(
    episode: Episode,
    *,
    repo_root: Path,
    eval_dir: Path,
    art_dir: Path,
    engine_url: str,
    model: str,
    prior_feedback_path: Path,
    iteration: int,
    analyze_skip_llm: bool = False,
    analyze_timeout: float = 3600.0,
    progress: LoopProgress | None = None,
    run_retest_agent: bool = False,
    spec: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Fresh copy + re-analyze with updated product; probe (optional agent score)."""
    if progress:
        progress.phase("retest", f"copy {episode.id}")
    workspace = materialize_episode(
        episode,
        repo_root=repo_root,
        subjects_dir=eval_dir / "subjects",
        mode="copy",
    )
    lock_path = eval_dir / "artifacts" / ".engine.lock"
    if progress:
        progress.phase("retest", "restart + analyze")
    with EngineLock(lock_path):
        reset_info = reset_for_episode(
            workspace,
            engine_url=engine_url,
            compose_dir=repo_root,
            restart=True,
            label=f"loop-retest-{episode.id}",
            analyze=True,
            analyze_skip_llm=analyze_skip_llm,
            analyze_timeout=analyze_timeout,
            on_progress=_progress_analyze_cb(progress),
        )
    write_json(art_dir / "retest_engine_reset.json", reset_info)
    if not _analyze_succeeded(reset_info):
        return {
            "ok": False,
            "error": "retest_analyze_failed",
            "engine": reset_info,
            "score": 0.0,
            "passed": False,
        }

    # Deterministic planner probes — pin to this analyze slug (not janus-bonus-* substring).
    slug = analyze_run_slug(reset_info.get("analyze") if isinstance(reset_info, dict) else None)
    if not slug:
        probe_info = {
            "ok": False,
            "failures": ["missing_analyze_slug"],
            "notes": [],
            "slug": None,
        }
        queue_items: list[dict[str, Any]] = []
        probe_slug = None
    else:
        probe_timing: dict[str, Any] = {}
        with record_duration("fetch_changesets_for_slug_s", probe_timing):
            probe_slug, queue_items = fetch_changesets_for_slug(
                url=engine_url, slug=slug
            )
        with record_duration("probe_queue_changesets_s", probe_timing):
            probe_info = probe_queue_changesets(
                queue_items,
                expect_coverage_prep=spec_expect_coverage_prep(spec),
            )
        if isinstance(probe_info, dict):
            probe_info["timing"] = probe_timing
    prior_blob: dict[str, Any] = {}
    if prior_feedback_path.is_file():
        try:
            loaded_prior = json.loads(prior_feedback_path.read_text(encoding="utf-8"))
            if isinstance(loaded_prior, dict):
                prior_blob = loaded_prior
        except json.JSONDecodeError:
            prior_blob = {}
    sticky_ids = sticky_changeset_ids(prior_blob, queue_items)
    planner_unchanged = bool(sticky_ids)
    probe_info = apply_spec_probes(
        probe_info,
        queue_items,
        spec,
        planner_unchanged=planner_unchanged,
        sticky_ids=sticky_ids,
    )
    probe_info = {
        **probe_info,
        "slug": probe_slug,
        "sticky_ids": sticky_ids,
        "planner_unchanged": planner_unchanged,
        "spec_coverage_prep": spec_expect_coverage_prep(spec),
        "spec_targets_planner": spec_targets_planner(spec),
    }
    write_json(art_dir / "retest_probe.json", probe_info)

    agent: dict[str, Any] | None = None
    retest_blob: dict[str, Any] = {}
    scored: dict[str, Any]
    if run_retest_agent:
        checklist = workspace / "prior_feedback.json"
        checklist.write_text(
            prior_feedback_path.read_text(encoding="utf-8"), encoding="utf-8"
        )
        agent_timing: dict[str, Any] = {}
        with record_duration("retest_agent_s", agent_timing):
            agent = run_local_agent(
                prompt=retest_prompt(
                    eval_dir=eval_dir,
                    episode_id=episode.id,
                    engine_url=engine_url,
                    prior_feedback_path=checklist.resolve(),
                    iteration=iteration,
                ),
                cwd=workspace,
                model=model,
                mcp_servers=mcp_autorefactor_stdio(
                    remote_url=engine_url, workspace=workspace
                ),
                on_heartbeat=_progress_heartbeat(progress),
            )
        _ensure_duration(agent, float(agent_timing.get("retest_agent_s") or 0.0))
        write_json(art_dir / "retest_run.json", agent)
        _copy_if_exists(workspace / "retest.json", art_dir / "retest.json")
        rt_path = art_dir / "retest.json"
        if rt_path.is_file():
            try:
                loaded = json.loads(rt_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    retest_blob = loaded
            except json.JSONDecodeError:
                retest_blob = {"error": "invalid_retest_json"}
        prior = load_prior_findings(prior_feedback_path)
        scored = score_retest(retest_blob, prior)
        scored["llm_passed"] = float(scored.get("score") or 0) >= RETEST_PASS_SCORE
        scored["mode"] = "agent"
    else:
        scored = {
            "ok": bool(probe_info.get("ok")),
            "score": 1.0 if probe_info.get("ok") else 0.0,
            "mode": "probe",
        }

    scored["planner_unchanged"] = planner_unchanged
    scored["sticky_ids"] = sticky_ids
    scored["probe_ok"] = probe_info.get("ok")
    probe_ok = bool(probe_info.get("ok"))
    if run_retest_agent:
        passed = probe_ok and bool(scored.get("llm_passed"))
    else:
        passed = probe_ok
        scored["ok"] = probe_ok
        scored["score"] = 1.0 if probe_ok else 0.0
    write_json(art_dir / "retest_score.json", scored)
    return {
        "ok": True,
        "agent": agent,
        "retest": retest_blob,
        "score": scored,
        "probe": probe_info,
        "planner_unchanged": planner_unchanged,
        "sticky_ids": sticky_ids,
        "passed": passed,
        "engine": reset_info,
    }


def _run_self_improve_loop_body(
    *,
    iterations: int,
    root: Path,
    eval_dir: Path,
    engine_url: str,
    model: str,
    episodes: list[Episode],
    all_episodes: bool,
    materialize_mode: str,
    skip_agents: bool,
    skip_rebuild: bool,
    skip_retest: bool,
    strict: bool,
    require_engine: bool,
    analyze: bool,
    analyze_skip_llm: bool,
    analyze_timeout: float,
    max_alpha_changes: int,
    progress: LoopProgress | None,
    alpha_timeout: float = DEFAULT_ALPHA_TIMEOUT_S,
    run_retest_agent: bool = False,
) -> dict[str, Any]:
    loop_root = eval_dir / "artifacts" / "loop"
    loop_root.mkdir(parents=True, exist_ok=True)

    ensure_info: dict[str, Any] | None = None
    if require_engine:
        if progress:
            progress.phase("ensure", "compose up --build autorefactor")
        lock_path = eval_dir / "artifacts" / ".engine.lock"
        with EngineLock(lock_path):
            ensure_info = ensure_engine(root, engine_url=engine_url)
        write_json(loop_root / "ensure_engine.json", ensure_info)

    iteration_results: list[dict[str, Any]] = []
    sticky_episode_id: str | None = None
    heartbeat = _progress_heartbeat(progress)

    for i in range(1, iterations + 1):
        iter_dir = loop_root / f"iter-{i:03d}"
        iter_dir.mkdir(parents=True, exist_ok=True)
        persona_files = (
            [select_persona(i)] if not all_episodes else list(USER_PERSONAS)
        )
        selected = select_episodes_for_iteration(
            episodes,
            iteration=i,
            all_episodes=all_episodes,
            sticky_episode_id=sticky_episode_id,
        )
        if progress:
            progress.set_context(
                iteration=i,
                episode=selected[0].id if selected else "",
                persona=(
                    persona_files[0]
                    if len(persona_files) == 1
                    else f"{len(persona_files)} personas"
                ),
            )
        stim_results: list[dict[str, Any]] = []
        feedback_paths: list[Path] = []

        for ep in selected:
            ep_art = iter_dir / ep.id
            ep_art.mkdir(parents=True, exist_ok=True)
            prior_by_persona, prior_spec, last_retest = _load_prior_critic_memory(
                loop_root=loop_root,
                iteration=i,
                episode_id=ep.id,
                persona_files=persona_files,
            )
            stim = run_stimulus_critic(
                ep,
                repo_root=root,
                eval_dir=eval_dir,
                art_dir=ep_art,
                engine_url=engine_url,
                model=model,
                materialize_mode=materialize_mode,
                skip_agents=skip_agents,
                analyze=analyze,
                analyze_skip_llm=analyze_skip_llm,
                analyze_timeout=analyze_timeout,
                persona_files=persona_files,
                progress=progress,
                prior_by_persona=prior_by_persona,
                prior_spec=prior_spec,
                last_retest=last_retest,
            )
            stim_results.append(stim)
            for walk in stim.get("walks") or []:
                wp = Path(walk.get("feedback_path") or "")
                if wp.is_file():
                    feedback_paths.append(wp)

        if skip_agents:
            iter_payload: dict[str, Any] = {
                "iteration": i,
                "stimulus": stim_results,
                "skipped_agents": True,
                "personas": persona_files,
            }
            write_json(iter_dir / "summary.json", iter_payload)
            iteration_results.append(iter_payload)
            rebuild_for_timing: dict[str, Any] | None = None
            if not skip_rebuild:
                if progress:
                    progress.phase("rebuild", "compose restart autorefactor")
                rebuild = rebuild_engine(root, engine_url=engine_url)
                iter_payload["rebuild"] = rebuild
                rebuild_for_timing = rebuild
                write_json(iter_dir / "summary.json", iter_payload)

            timing_payload = _iteration_timing_payload(
                iteration=i,
                stim_results=stim_results,
                retest_results=None,
                rebuild_info=rebuild_for_timing,
                ensure_info=ensure_info,
                agents={},
            )
            _write_iteration_timing_json(iter_dir, timing_payload)
            if progress:
                progress.finish_iteration(_iteration_row(iter_payload))
            continue

        product_use_flags = [
            bool((s.get("feedback_gate") or {}).get("product_use_ok"))
            for s in stim_results
            if not s.get("skipped_agents")
        ]
        product_use_ok = any(product_use_flags)

        finding_meta: dict[str, dict] = {}
        blobs: list[dict[str, Any]] = []
        for fp in feedback_paths:
            blob = _load_feedback_blob(fp)
            if blob is None:
                continue
            blobs.append(blob)
            finding_meta.update(_finding_meta_from_feedback(blob))
        if blobs:
            merged = _merge_feedback_blobs(
                blobs, iteration=i, product_use_ok=product_use_ok
            )
            triage_feedback = iter_dir / "feedback_merged.json"
            write_json(triage_feedback, merged)
        else:
            triage_feedback = iter_dir / "feedback_merged.json"
            write_json(
                triage_feedback,
                {
                    "episode_id": f"iter-{i}",
                    "source": "product_use",
                    "summary": "no critic feedback.json produced",
                    "walks": [],
                    "findings": [],
                },
            )
            product_use_ok = False

        triage: dict[str, Any]
        spec: dict[str, Any]
        if not product_use_ok:
            if progress:
                progress.phase("triage", "skipped")
            triage = {
                "ok": True,
                "skipped": True,
                "reason": "feedback_not_from_product_use",
            }
            write_json(iter_dir / "triage_run.json", triage)
            spec = {
                "summary": "",
                "changes": [],
                "stop_reason": "feedback_not_from_product_use",
            }
            write_json(iter_dir / "alpha_spec.sanitized.json", spec)
        else:
            if progress:
                progress.phase("triage", "rank findings")
            triage_timing: dict[str, Any] = {}
            with record_duration("triage_s", triage_timing):
                triage = run_local_agent(
                    prompt=_triage_prompt(
                        feedback_path=triage_feedback.resolve(),
                        persona=_read_persona(eval_dir, "triage_alpha.md"),
                        iteration=i,
                        product_use_ok=product_use_ok,
                    ),
                    cwd=root,
                    model=model,
                    on_heartbeat=heartbeat,
                )
            _ensure_duration(triage, float(triage_timing.get("triage_s") or 0.0))
            write_json(iter_dir / "triage_run.json", triage)
            _copy_if_exists(root / "alpha_spec.json", iter_dir / "alpha_spec.json")
            spec = _sanitize_alpha_spec(
                root, finding_meta=finding_meta, max_changes=max_alpha_changes
            )
            write_json(iter_dir / "alpha_spec.sanitized.json", spec)

        alpha: dict[str, Any]
        if not product_use_ok or not (spec.get("changes") or []):
            if progress:
                progress.phase("alpha", "skipped")
            alpha = {
                "ok": True,
                "skipped": True,
                "reason": (
                    "feedback_not_from_product_use"
                    if not product_use_ok
                    else "empty_alpha_spec"
                ),
            }
            write_json(iter_dir / "alpha_run.json", alpha)
        else:
            if progress:
                progress.phase("alpha", "patch product")
            alpha_timing: dict[str, Any] = {}
            with record_duration("alpha_s", alpha_timing):
                alpha = run_local_agent(
                    prompt=_alpha_prompt(
                        persona=_read_persona(eval_dir, "alpha_improver.md"),
                        iteration=i,
                    ),
                    cwd=root,
                    model=model,
                    on_heartbeat=heartbeat,
                    timeout_s=alpha_timeout,
                )
            _ensure_duration(alpha, float(alpha_timing.get("alpha_s") or 0.0))
            write_json(iter_dir / "alpha_run.json", alpha)
            _copy_if_exists(root / "alpha_result.json", iter_dir / "alpha_result.json")

        if progress:
            progress.phase("smoke", "pytest")
        smoke = run_smoke_tests(root)
        write_json(iter_dir / "smoke.json", smoke)
        smoke_repairs: list[dict[str, Any]] = []
        if not smoke["ok"] and not skip_agents:
            if progress:
                progress.phase("repair", "fix failed smoke")
            smoke, smoke_repairs = repair_failed_smoke(
                repo_root=root,
                eval_dir=eval_dir,
                iter_dir=iter_dir,
                iteration=i,
                model=model,
                smoke=smoke,
                on_heartbeat=heartbeat,
                timeout_s=alpha_timeout,
            )
            write_json(iter_dir / "smoke_repairs.json", {"attempts": smoke_repairs})

        smoke_ok = bool(smoke.get("ok"))
        if strict and not smoke_ok:
            raise RuntimeError(
                f"smoke tests failed at iteration {i} after "
                f"{len(smoke_repairs)} repair attempt(s)"
            )

        rebuild_info: dict[str, Any] | None = None
        retest_info: dict[str, Any] | None = None
        retest_episode_id: str | None = None
        alpha_applied = bool(spec.get("changes")) and not alpha.get("skipped")
        # Never rebuild or retest a product that still fails smoke.
        if not smoke_ok:
            if progress:
                progress.phase("rebuild", "skipped smoke_failed")
            rebuild_info = {
                "ok": False,
                "skipped": True,
                "reason": "smoke_failed",
            }
            write_json(iter_dir / "rebuild.json", rebuild_info)
        elif not alpha_applied:
            if progress:
                progress.phase("rebuild", "skipped alpha_noop")
            rebuild_info = {
                "ok": True,
                "skipped": True,
                "reason": "alpha_noop",
            }
            write_json(iter_dir / "rebuild.json", rebuild_info)
        elif not skip_rebuild:
            if progress:
                progress.phase("rebuild", "compose restart autorefactor")
            lock_path = eval_dir / "artifacts" / ".engine.lock"
            with EngineLock(lock_path):
                rebuild_info = rebuild_engine(root, engine_url=engine_url)
            write_json(iter_dir / "rebuild.json", rebuild_info)

        retest_results: list[dict[str, Any]] = []
        if smoke_ok and not skip_retest and product_use_ok and alpha_applied:
            sticky_episode_id = None
            for ep in selected:
                ep_art = iter_dir / ep.id
                prior = ep_art / "feedback.json"
                if not prior.is_file():
                    continue
                one = run_retest_episode(
                    ep,
                    repo_root=root,
                    eval_dir=eval_dir,
                    art_dir=ep_art,
                    engine_url=engine_url,
                    model=model,
                    prior_feedback_path=prior,
                    iteration=i,
                    analyze_skip_llm=analyze_skip_llm,
                    analyze_timeout=analyze_timeout,
                    progress=progress,
                    run_retest_agent=run_retest_agent,
                    spec=spec,
                )
                retest_results.append({"episode_id": ep.id, **one})
                if retest_episode_id is None:
                    retest_episode_id = ep.id
                    retest_info = one
                if not one.get("passed") and sticky_episode_id is None:
                    sticky_episode_id = ep.id
            if retest_results:
                n = len(retest_results)
                ok_n = sum(1 for r in retest_results if r.get("passed"))
                if retest_info is None:
                    retest_info = dict(retest_results[0])
                else:
                    retest_info = dict(retest_info)
                retest_info["passed"] = ok_n == n
                retest_info["episodes_passed"] = ok_n
                retest_info["episodes_total"] = n
                write_json(
                    iter_dir / "retest_summary.json", {"episodes": retest_results}
                )
        elif not alpha_applied or not smoke_ok:
            sticky_episode_id = None

        iter_payload = {
            "iteration": i,
            "personas": persona_files,
            "stimulus": stim_results,
            "triage": triage,
            "alpha": alpha,
            "spec": spec,
            "smoke": smoke,
            "smoke_repairs": smoke_repairs,
            "rebuild": rebuild_info,
            "retest": retest_info,
            "retests": retest_results,
            "sticky_next_episode": sticky_episode_id,
        }
        write_json(iter_dir / "summary.json", iter_payload)
        timing_payload = _iteration_timing_payload(
            iteration=i,
            stim_results=stim_results,
            retest_results=retest_results,
            rebuild_info=rebuild_info,
            ensure_info=ensure_info,
            agents=_agents_timing(
                stim_results=stim_results,
                triage=triage,
                alpha=alpha,
                retest_info=retest_info,
            ),
        )
        _write_iteration_timing_json(iter_dir, timing_payload)
        iteration_results.append(iter_payload)
        if progress:
            progress.finish_iteration(_iteration_row(iter_payload))

        if strict:
            if triage and not triage.get("ok", True) and not triage.get("skipped"):
                raise RuntimeError(f"agent failed at iteration {i}: {triage}")
            alpha_timeout_hit = bool(
                alpha.get("timed_out")
                or str(alpha.get("status") or "").lower() == "timeout"
            )
            if (
                alpha
                and not alpha.get("ok", True)
                and not alpha.get("skipped")
                and not (alpha_timeout_hit and smoke_ok)
            ):
                raise RuntimeError(f"agent failed at iteration {i}: {alpha}")
            if retest_results and any(not r.get("passed") for r in retest_results):
                failed = [r.get("episode_id") for r in retest_results if not r.get("passed")]
                raise RuntimeError(
                    f"retest failed at iteration {i}: episodes={failed} "
                    f"passed={sum(1 for r in retest_results if r.get('passed'))}"
                    f"/{len(retest_results)}"
                )

    summary = {
        "ok": True,
        "iterations": iterations,
        "ensure_engine": ensure_info,
        "results": iteration_results,
    }
    write_json(loop_root / "summary.json", summary)
    return summary


def run_self_improve_loop(
    *,
    iterations: int = 3,
    repo_root: Path | None = None,
    engine_url: str = "http://127.0.0.1:8765",
    model: str = "composer-2.5",
    episode_ids: list[str] | None = None,
    use_local_corpus: bool = True,
    all_episodes: bool = True,
    materialize_mode: str = "copy",
    skip_agents: bool = False,
    skip_rebuild: bool = False,
    skip_retest: bool = False,
    strict: bool = False,
    require_engine: bool = True,
    analyze: bool = True,
    analyze_skip_llm: bool = False,
    analyze_timeout: float = 3600.0,
    max_alpha_changes: int = MAX_ALPHA_CHANGES,
    alpha_timeout: float = DEFAULT_ALPHA_TIMEOUT_S,
    run_retest_agent: bool = False,
    progress: LoopProgress | None = None,
) -> dict[str, Any]:
    if iterations < 1:
        raise ValueError("iterations must be >= 1")

    root = repo_root or _repo_root()
    eval_dir = _eval_dir(root)
    load_secret_files(eval_dir / "secrets.local.env", root / ".env", override=False)
    if not skip_agents and not resolve_cursor_api_key(repo_root=root, eval_dir=eval_dir):
        raise RuntimeError(
            "CURSOR_API_KEY missing. Add to eval/secrets.local.env or repo .env."
        )

    if not skip_agents and not analyze:
        raise RuntimeError(
            "loop with agents requires analyze=True. "
            "Do not pass --no-analyze unless --skip-agents."
        )

    corpus = load_corpus(eval_dir=eval_dir, use_local=use_local_corpus)
    episodes = list(corpus.episodes)
    if episode_ids:
        want = set(episode_ids)
        episodes = [e for e in episodes if e.id in want]
    if not episodes:
        raise RuntimeError("no corpus episodes to run")

    if progress:
        progress.start(iterations)
    try:
        return _run_self_improve_loop_body(
            iterations=iterations,
            root=root,
            eval_dir=eval_dir,
            engine_url=engine_url,
            model=model,
            episodes=episodes,
            all_episodes=all_episodes,
            materialize_mode=materialize_mode,
            skip_agents=skip_agents,
            skip_rebuild=skip_rebuild,
            skip_retest=skip_retest,
            strict=strict,
            require_engine=require_engine,
            analyze=analyze,
            analyze_skip_llm=analyze_skip_llm,
            analyze_timeout=analyze_timeout,
            max_alpha_changes=max_alpha_changes,
            alpha_timeout=alpha_timeout,
            run_retest_agent=run_retest_agent,
            progress=progress,
        )
    finally:
        if progress:
            progress.stop()

