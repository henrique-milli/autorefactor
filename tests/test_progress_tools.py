from __future__ import annotations

import time
from pathlib import Path

from autorefactor.changesets.models import Changeset, ChangesetStatus, ChangesetTarget
from autorefactor.jobs import start_analyze_job
from autorefactor.mcp import tools as T
from autorefactor.mcp.remote import reset_proxy_state
from autorefactor.queue import save_queue


def test_get_progress_without_id_resolves_active(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    ws = tmp_path / "proj"
    ws.mkdir()
    (ws / "a.py").write_text("print(1)\n", encoding="utf-8")
    monkeypatch.setenv("AUTOREFACTOR_MCP_WORKSPACE", str(ws))
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    release = {"go": False}

    def runner(workspace, out, skip_llm, describe_mode, focus, progress=None):  # noqa: ANN001
        if progress:
            progress("embed", 45, "embedding")
        while not release["go"]:
            time.sleep(0.02)
        return {"ok": True}

    started = start_analyze_job(
        workspace=str(ws),
        out=str(out_dir),
        skip_llm=True,
        describe_mode=None,
        focus=None,
        lock_key=str(out_dir.resolve()),
        runner=runner,
    )
    assert started["status"] == "running"

    progress = T.tool_get_progress(workspace=str(ws), out=str(out_dir))
    assert progress["ok"] is True
    assert progress["job_id"] == started["job_id"]
    assert progress["status"] == "running"
    assert isinstance(progress.get("events"), list)

    alias = T.tool_get_analyze_job(started["job_id"])
    assert alias["job_id"] == started["job_id"]
    assert "events" in alias

    listed = T.tool_list_jobs(limit=10)
    assert listed["ok"] is True
    assert any(j["job_id"] == started["job_id"] for j in listed["jobs"])

    release["go"] = True
    deadline = time.time() + 3
    while progress.get("status") == "running" and time.time() < deadline:
        time.sleep(0.05)
        progress = T.tool_get_progress(job_id=started["job_id"])
    assert progress["status"] == "done"


def test_get_status_counts_and_active_job(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    ws = tmp_path / "proj"
    ws.mkdir()
    (ws / "a.py").write_text("print(1)\n", encoding="utf-8")
    monkeypatch.setenv("AUTOREFACTOR_MCP_WORKSPACE", str(ws))
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    save_queue(
        out_dir,
        [
            Changeset(
                id="cs1",
                title="one",
                action="split",
                brief="b",
                rationale="r",
                confidence=0.9,
                targets=[ChangesetTarget(repo="a", path="A.java")],
                status=ChangesetStatus.PENDING,
            ),
            Changeset(
                id="cs2",
                title="two",
                action="merge",
                brief="b",
                rationale="r",
                confidence=0.8,
                targets=[ChangesetTarget(repo="a", path="B.java")],
                status=ChangesetStatus.DONE,
            ),
        ],
    )
    report = out_dir / "report"
    report.mkdir()
    (report / "report.md").write_text("# ok\n", encoding="utf-8")

    release = {"go": False}

    def runner(workspace, out, skip_llm, describe_mode, focus, progress=None):  # noqa: ANN001
        while not release["go"]:
            time.sleep(0.02)
        return {"ok": True}

    started = start_analyze_job(
        workspace=str(ws),
        out=str(out_dir),
        skip_llm=True,
        describe_mode=None,
        focus=None,
        lock_key=str(out_dir.resolve()),
        runner=runner,
    )

    status = T.tool_get_status(workspace=str(ws), out=str(out_dir))
    assert status["ok"] is True
    assert status["changesets"] == 2
    assert status["by_status"]["pending"] == 1
    assert status["by_status"]["done"] == 1
    assert status["report"] is True
    assert status["active_job"] is not None
    assert status["active_job"]["job_id"] == started["job_id"]

    release["go"] = True
    deadline = time.time() + 3
    progress = T.tool_get_progress(job_id=started["job_id"])
    while progress.get("status") == "running" and time.time() < deadline:
        time.sleep(0.05)
        progress = T.tool_get_progress(job_id=started["job_id"])
    assert progress["status"] == "done"


def test_proxy_list_jobs_and_get_progress(tmp_path: Path, monkeypatch) -> None:
    reset_proxy_state()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "a.py").write_text("print(1)\n", encoding="utf-8")
    monkeypatch.setenv("AUTOREFACTOR_REMOTE_URL", "http://remote.test:8765")
    monkeypatch.setenv("AUTOREFACTOR_MCP_WORKSPACE", str(proj))

    calls: list[tuple[str, dict | None]] = []

    def fake_ensure(**kwargs):  # noqa: ANN003
        calls.append(("ensure", kwargs))
        return {"ok": True, "file_count": 1}

    def fake_call(name: str, arguments=None, **kwargs):  # noqa: ANN001
        calls.append((name, arguments))
        return {"ok": True, "proxied": True, "tool": name, "args": arguments}

    monkeypatch.setattr("autorefactor.mcp.remote.ensure_remote_workspace", fake_ensure)
    monkeypatch.setattr("autorefactor.mcp.remote.call_remote_tool", fake_call)

    listed = T.tool_list_jobs(limit=5)
    assert listed["tool"] == "list_jobs"
    assert any(c[0] == "list_jobs" for c in calls)

    progress = T.tool_get_progress(workspace=None, out=None)
    assert progress["tool"] == "get_progress"
    progress_args = [c[1] for c in calls if c[0] == "get_progress"][-1]
    assert progress_args.get("workspace") == "active"

    by_id = T.tool_get_progress(job_id="abc123")
    assert by_id["tool"] == "get_progress"
    by_id_args = [c[1] for c in calls if c[0] == "get_progress"][-1]
    assert "workspace" not in by_id_args or by_id_args.get("workspace") in {None, "active"}

    status = T.tool_get_status()
    assert status["tool"] == "get_status"

    reset_proxy_state()
    monkeypatch.delenv("AUTOREFACTOR_REMOTE_URL", raising=False)
