from __future__ import annotations

from pathlib import Path

from autorefactor.mcp import tools as T
from autorefactor.mcp.remote import reset_proxy_state


def test_proxy_analyze_syncs_then_forwards(tmp_path: Path, monkeypatch) -> None:
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
        return {"ok": True, "workspace": "active", "proxied": True, "tool": name}

    monkeypatch.setattr("autorefactor.mcp.remote.ensure_remote_workspace", fake_ensure)
    monkeypatch.setattr("autorefactor.mcp.remote.call_remote_tool", fake_call)

    result = T.tool_analyze(workspace=None, skip_llm=True)
    assert result["ok"] is True
    assert result["proxied"] is True
    assert any(c[0] == "ensure" for c in calls)
    analyze_calls = [c for c in calls if c[0] == "analyze"]
    assert analyze_calls
    assert analyze_calls[0][1].get("workspace") == "active"

    reset_proxy_state()
    monkeypatch.delenv("AUTOREFACTOR_REMOTE_URL", raising=False)


def test_proxy_prepare_skips_local_sync(tmp_path: Path, monkeypatch) -> None:
    reset_proxy_state()
    monkeypatch.setenv("AUTOREFACTOR_REMOTE_URL", "http://remote.test:8765")
    sync_calls: list[str] = []

    def fake_ensure(**kwargs):  # noqa: ANN003
        sync_calls.append("sync")
        return {"ok": True}

    def fake_call(name: str, arguments=None, **kwargs):  # noqa: ANN001
        return {"ok": True, "tool": name, "git_url": (arguments or {}).get("git_url")}

    monkeypatch.setattr("autorefactor.mcp.remote.ensure_remote_workspace", fake_ensure)
    monkeypatch.setattr("autorefactor.mcp.remote.call_remote_tool", fake_call)

    result = T.tool_prepare_workspace("https://github.com/org/repo.git", ref="main")
    assert result["ok"] is True
    assert result["tool"] == "prepare_workspace"
    assert sync_calls == []
    reset_proxy_state()
    monkeypatch.delenv("AUTOREFACTOR_REMOTE_URL", raising=False)


def test_proxy_analyze_does_not_pull(tmp_path: Path, monkeypatch) -> None:
    reset_proxy_state()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "a.py").write_text("print(1)\n", encoding="utf-8")
    monkeypatch.setenv("AUTOREFACTOR_REMOTE_URL", "http://remote.test:8765")
    monkeypatch.setenv("AUTOREFACTOR_MCP_WORKSPACE", str(proj))

    calls: list[str] = []

    def fake_ensure(**kwargs):  # noqa: ANN003
        calls.append("ensure")
        return {"ok": True, "file_count": 1}

    def fake_call(name: str, arguments=None, **kwargs):  # noqa: ANN001
        calls.append(name)
        return {"ok": True, "workspace": "active", "describe_mode": "raw"}

    def fake_pull(*, paths=None):  # noqa: ANN001
        calls.append("pull")
        return {"ok": True, "pulled_paths": paths or [], "docs_pulled": 0}

    monkeypatch.setattr("autorefactor.mcp.remote.ensure_remote_workspace", fake_ensure)
    monkeypatch.setattr("autorefactor.mcp.remote.call_remote_tool", fake_call)
    monkeypatch.setattr("autorefactor.mcp.remote.pull_remote_workspace", fake_pull)

    result = T.tool_analyze(workspace=None, skip_llm=True, describe_mode="raw")
    assert result["ok"] is True
    assert "pull" not in calls
    assert "docs_pulled" not in result

    reset_proxy_state()
    monkeypatch.delenv("AUTOREFACTOR_REMOTE_URL", raising=False)


def test_proxy_confirm_changeset_pulls_back(tmp_path: Path, monkeypatch) -> None:
    reset_proxy_state()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "a.py").write_text("print(1)\n", encoding="utf-8")
    monkeypatch.setenv("AUTOREFACTOR_REMOTE_URL", "http://remote.test:8765")
    monkeypatch.setenv("AUTOREFACTOR_MCP_WORKSPACE", str(proj))

    calls: list[str] = []

    def fake_ensure(**kwargs):  # noqa: ANN003
        calls.append("ensure")
        return {"ok": True, "file_count": 1}

    def fake_call(name: str, arguments=None, **kwargs):  # noqa: ANN001
        calls.append(name)
        return {
            "ok": True,
            "workspace": "active",
            "docs_written": 1,
            "touched_paths": ["a.py"],
        }

    def fake_pull(*, paths=None):  # noqa: ANN001
        calls.append("pull")
        assert paths == ["a.py"]
        return {"ok": True, "pulled_paths": ["a.py"], "docs_pulled": 1}

    monkeypatch.setattr("autorefactor.mcp.remote.ensure_remote_workspace", fake_ensure)
    monkeypatch.setattr("autorefactor.mcp.remote.call_remote_tool", fake_call)
    monkeypatch.setattr("autorefactor.mcp.remote.pull_remote_workspace", fake_pull)

    result = T.tool_confirm_changeset(
        changeset_id="cs1",
        decision="proceed",
        options=["write_docs"],
        workspace=None,
    )
    assert result["ok"] is True
    assert result["docs_pulled"] == 1
    assert result["pulled_paths"] == ["a.py"]
    assert "ensure" in calls and "confirm_changeset" in calls and "pull" in calls

    reset_proxy_state()
    monkeypatch.delenv("AUTOREFACTOR_REMOTE_URL", raising=False)


def test_sync_workspace_tool_force_uploads(tmp_path: Path, monkeypatch) -> None:
    reset_proxy_state()
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.setenv("AUTOREFACTOR_REMOTE_URL", "http://remote.test:8765")
    monkeypatch.setenv("AUTOREFACTOR_MCP_WORKSPACE", str(proj))

    def fake_ensure(*, force: bool = False):
        assert force is True
        return {"ok": True, "file_count": 3, "root": str(proj)}

    monkeypatch.setattr("autorefactor.mcp.remote.ensure_remote_workspace", fake_ensure)
    result = T.tool_sync_workspace()
    assert result["ok"] is True
    assert result["synced"] is True
    reset_proxy_state()


def test_sync_workspace_unavailable_without_remote(monkeypatch) -> None:
    reset_proxy_state()
    monkeypatch.delenv("AUTOREFACTOR_REMOTE_URL", raising=False)
    result = T.tool_sync_workspace()
    assert result["ok"] is False
    assert result["error"] == "sync_unavailable"


def test_proxy_resolves_eval_subject_from_monorepo_cwd(
    tmp_path: Path, monkeypatch
) -> None:
    """Monorepo cwd under eval/subjects/janus-bonus packs episode, not parent tree."""
    reset_proxy_state()
    repo = tmp_path / "autorefactor"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[project]\nname='ar'\n", encoding="utf-8")
    (repo / "src" / "autorefactor").mkdir(parents=True)
    episode = repo / "eval" / "subjects" / "janus-bonus"
    episode.mkdir(parents=True)
    (episode / ".ar_eval_snapshot.json").write_text("{}", encoding="utf-8")
    for i in range(88):
        (episode / f"f{i}.txt").write_text("x\n", encoding="utf-8")

    monkeypatch.chdir(episode)
    monkeypatch.setenv("AUTOREFACTOR_REMOTE_URL", "http://remote.test:8765")

    from autorefactor.mcp.remote import _local_workspace_root

    root = _local_workspace_root()
    assert root.name == "janus-bonus"
    assert root == episode.resolve()


def test_proxy_get_next_changeset_tolerates_file_count_drift(
    tmp_path: Path, monkeypatch
) -> None:
    """Matching label+source with ±3 file_count drift proceeds without workspace_mismatch."""
    reset_proxy_state()
    episode = tmp_path / "eval" / "subjects" / "janus-bonus"
    episode.mkdir(parents=True)
    (episode / ".ar_eval_snapshot.json").write_text("{}", encoding="utf-8")
    (episode / "sample.py").write_text("x=1\n", encoding="utf-8")

    monkeypatch.chdir(episode)
    monkeypatch.setenv("AUTOREFACTOR_REMOTE_URL", "http://remote.test:8765")

    calls: list[tuple[str, dict | None]] = []
    sync_forces: list[bool] = []

    def fake_ensure(*, force: bool = False, **kwargs):  # noqa: ANN003
        sync_forces.append(force)
        return {
            "ok": True,
            "label": "janus-bonus",
            "source": str(episode),
            "file_count": 88,
        }

    def fake_remote():
        return {
            "label": "janus-bonus",
            "source": str(episode),
            "file_count": 85,
        }

    def fake_call(name: str, arguments=None, **kwargs):  # noqa: ANN001
        calls.append((name, arguments))
        return {"ok": True, "tool": name, "changeset_id": "cs1"}

    monkeypatch.setattr("autorefactor.mcp.remote.ensure_remote_workspace", fake_ensure)
    monkeypatch.setattr("autorefactor.mcp.remote._remote_workspace_identity", fake_remote)
    monkeypatch.setattr("autorefactor.mcp.remote.call_remote_tool", fake_call)

    from autorefactor.mcp.remote import proxy_tool

    result = proxy_tool("get_next_changeset", {})
    assert result["ok"] is True
    assert result.get("error") != "workspace_mismatch"
    assert any(c[0] == "get_next_changeset" for c in calls)
    reset_proxy_state()
    monkeypatch.delenv("AUTOREFACTOR_REMOTE_URL", raising=False)


def test_local_workspace_root_honors_env(tmp_path: Path, monkeypatch) -> None:
    reset_proxy_state()
    ws = tmp_path / "eval" / "subjects" / "janus-bonus"
    ws.mkdir(parents=True)
    monkeypatch.setenv("AUTOREFACTOR_MCP_WORKSPACE", str(ws))
    monkeypatch.delenv("AUTOREFACTOR_REMOTE_URL", raising=False)

    from autorefactor.mcp.remote import _local_workspace_root

    assert _local_workspace_root() == ws.resolve()
    reset_proxy_state()


def test_ensure_remote_skips_product_repo(tmp_path: Path, monkeypatch) -> None:
    reset_proxy_state()
    repo = tmp_path / "autorefactor"
    (repo / "src" / "autorefactor").mkdir(parents=True)
    (repo / "pyproject.toml").write_text("[project]\nname='ar'\n", encoding="utf-8")
    monkeypatch.chdir(repo)
    monkeypatch.delenv("AUTOREFACTOR_MCP_WORKSPACE", raising=False)
    monkeypatch.setenv("AUTOREFACTOR_REMOTE_URL", "http://remote.test:8765")

    def boom(*_args, **_kwargs):  # noqa: ANN001
        raise AssertionError("must not POST the product repo")

    monkeypatch.setattr("autorefactor.mcp.remote.sync_workspace_http", boom)

    from autorefactor.mcp.remote import ensure_remote_workspace

    result = ensure_remote_workspace(force=True)
    assert result["ok"] is True
    assert result["skipped"] is True
    assert result["reason"] == "product_repo"
    reset_proxy_state()
    monkeypatch.delenv("AUTOREFACTOR_REMOTE_URL", raising=False)
