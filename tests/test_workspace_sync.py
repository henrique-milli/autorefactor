from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from autorefactor.workspace_sync import (
    clone_git_workspace,
    clone_git_workspaces,
    export_active_tar_gz,
    extract_tar_gz_into,
    import_tar_gz,
    list_workspace_files,
    pack_workspace_tar_gz,
    resolve_analyze_workspace,
    validate_git_url,
    workspace_status,
)


def test_pack_and_import_roundtrip(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    src = tmp_path / "proj"
    (src / "src").mkdir(parents=True)
    (src / "src" / "a.py").write_text("print(1)\n", encoding="utf-8")
    (src / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")

    buf_path = tmp_path / "w.tgz"
    meta = pack_workspace_tar_gz(src, buf_path)
    assert meta["file_count"] >= 2
    data = buf_path.read_bytes()
    result = import_tar_gz(data, label="proj", source=str(src))
    assert result["ok"] is True
    assert result["file_count"] >= 2
    active = resolve_analyze_workspace("active")
    assert (active / "src" / "a.py").read_text(encoding="utf-8") == "print(1)\n"
    assert (active / "dirty.txt").read_text(encoding="utf-8") == "uncommitted\n"
    status = workspace_status()
    assert status["synced"] is True


def test_resolve_prefers_mounted_workspace(tmp_path: Path, monkeypatch) -> None:
    mounted = tmp_path / "mounted"
    mounted.mkdir()
    (mounted / "a.py").write_text("x\n", encoding="utf-8")
    monkeypatch.setenv("AUTOREFACTOR_MCP_WORKSPACE", str(mounted))
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("AUTOREFACTOR_MCP_PREFER_ACTIVE", raising=False)
    assert resolve_analyze_workspace(None) == mounted.resolve()
    assert resolve_analyze_workspace(str(mounted)) == mounted.resolve()


def test_resolve_falls_back_to_cwd(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("AUTOREFACTOR_MCP_WORKSPACE", raising=False)
    monkeypatch.delenv("AUTOREFACTOR_MCP_PREFER_ACTIVE", raising=False)
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "a.py").write_text("x\n", encoding="utf-8")
    monkeypatch.chdir(proj)
    assert resolve_analyze_workspace(None) == proj.resolve()
    assert resolve_analyze_workspace(".") == proj.resolve()


def test_resolve_active_when_requested(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("AUTOREFACTOR_MCP_WORKSPACE", raising=False)
    active = home / ".autorefactor" / "workspaces" / "active"
    active.mkdir(parents=True)
    (active / "b.py").write_text("y\n", encoding="utf-8")
    assert resolve_analyze_workspace("active") == active.resolve()


def test_resolve_prefer_active_flag(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AUTOREFACTOR_MCP_PREFER_ACTIVE", "1")
    mounted = tmp_path / "mounted"
    mounted.mkdir()
    (mounted / "a.py").write_text("x\n", encoding="utf-8")
    monkeypatch.setenv("AUTOREFACTOR_MCP_WORKSPACE", str(mounted))
    active = home / ".autorefactor" / "workspaces" / "active"
    active.mkdir(parents=True)
    (active / "from-git.py").write_text("g\n", encoding="utf-8")
    assert resolve_analyze_workspace(None) == active.resolve()


def test_resolve_active_with_sync_meta_wins_over_mount(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("AUTOREFACTOR_MCP_PREFER_ACTIVE", raising=False)
    mounted = tmp_path / "mounted"
    mounted.mkdir()
    (mounted / "a.py").write_text("x\n", encoding="utf-8")
    monkeypatch.setenv("AUTOREFACTOR_MCP_WORKSPACE", str(mounted))
    active = home / ".autorefactor" / "workspaces" / "active"
    active.mkdir(parents=True)
    (active / "synced.py").write_text("s\n", encoding="utf-8")
    (active / ".autorefactor-sync.json").write_text(
        '{"label": "proj", "source": "/tmp/proj"}',
        encoding="utf-8",
    )
    assert resolve_analyze_workspace(None) == active.resolve()
    # Explicit mounted still uses the mount
    assert resolve_analyze_workspace("mounted") == mounted.resolve()


def test_resolve_workspace_info_echo(tmp_path: Path, monkeypatch) -> None:
    from autorefactor.workspace_sync import resolve_workspace_info

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("AUTOREFACTOR_MCP_PREFER_ACTIVE", raising=False)
    mounted = tmp_path / "mounted"
    mounted.mkdir()
    (mounted / "a.py").write_text("x\n", encoding="utf-8")
    monkeypatch.setenv("AUTOREFACTOR_MCP_WORKSPACE", str(mounted))
    info = resolve_workspace_info(None)
    assert info["resolved_via"] == "env"
    assert Path(info["workspace"]) == mounted.resolve()


def test_validate_git_url_rejects_local() -> None:
    with pytest.raises(RuntimeError, match="Local paths"):
        validate_git_url("/tmp/repo")
    with pytest.raises(RuntimeError, match="Local paths"):
        validate_git_url("file:///tmp/repo")
    with pytest.raises(RuntimeError, match="must be"):
        validate_git_url("not-a-url")
    assert validate_git_url("https://github.com/org/repo.git").startswith("https://")
    assert validate_git_url("git@github.com:org/repo.git").startswith("git@")


def test_validate_git_url_rejects_embedded_credentials() -> None:
    with pytest.raises(RuntimeError, match="AUTOREFACTOR_GIT_TOKEN"):
        validate_git_url("https://x-access-token:gho_secret@github.com/org/repo.git")
    with pytest.raises(RuntimeError, match="credentials"):
        validate_git_url("https://user:pass@github.com/org/repo.git")


def test_clone_git_workspace_mocked(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    def fake_run(cmd, check=False, capture_output=True, text=True, timeout=600):  # noqa: ANN001
        dest = Path(cmd[-1])
        dest.mkdir(parents=True)
        (dest / "src").mkdir()
        (dest / "src" / "Main.java").write_text("class Main {}\n", encoding="utf-8")
        (dest / ".git").mkdir()
        (dest / ".git" / "config").write_text("x\n", encoding="utf-8")
        return MagicMock(returncode=0, stderr="", stdout="")

    monkeypatch.setattr("autorefactor.workspace_sync.subprocess.run", fake_run)
    result = clone_git_workspace("https://github.com/example/demo.git", ref="main")
    assert result["ok"] is True
    assert result["source"] == "git"
    assert result["ref"] == "main"
    active = resolve_analyze_workspace("active")
    assert (active / "src" / "Main.java").is_file()
    assert not (active / ".git").exists()


def test_clone_git_workspaces_siblings(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    def fake_run(cmd, check=False, capture_output=True, text=True, timeout=600):  # noqa: ANN001
        dest = Path(cmd[-1])
        dest.mkdir(parents=True)
        (dest / "pom.xml").write_text("<project/>\n", encoding="utf-8")
        (dest / ".git").mkdir()
        return MagicMock(returncode=0, stderr="", stdout="")

    monkeypatch.setattr("autorefactor.workspace_sync.subprocess.run", fake_run)
    result = clone_git_workspaces(
        [
            "https://github.com/example/service-a.git",
            "https://github.com/example/service-b.git",
        ]
    )
    assert result["ok"] is True
    assert result["kind"] == "git_multi"
    assert result["repos"] == 2
    active = resolve_analyze_workspace("active")
    assert (active / "service-a" / "pom.xml").is_file()
    assert (active / "service-b" / "pom.xml").is_file()


def test_list_walk_without_git(tmp_path: Path) -> None:
    src = tmp_path / "plain"
    src.mkdir()
    (src / "f.txt").write_text("x", encoding="utf-8")
    files = list_workspace_files(src)
    assert "f.txt" in files


def test_list_workspace_includes_jacoco_xml_under_target(tmp_path: Path) -> None:
    src = tmp_path / "plain"
    src.mkdir()
    (src / "src" / "a.py").parent.mkdir(parents=True)
    (src / "src" / "a.py").write_text("x=1\n", encoding="utf-8")
    jacoco = src / "target" / "site" / "jacoco" / "jacoco.xml"
    jacoco.parent.mkdir(parents=True)
    jacoco.write_text("<report/>\n", encoding="utf-8")
    (src / "target" / "classes" / "A.class").parent.mkdir(parents=True)
    (src / "target" / "classes" / "A.class").write_bytes(b"xx")
    files = list_workspace_files(src)
    assert "src/a.py" in files
    assert any(f.endswith("jacoco.xml") for f in files)
    assert not any(f.endswith(".class") for f in files)


def test_export_active_and_extract_roundtrip(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    src = tmp_path / "proj"
    (src / "src").mkdir(parents=True)
    (src / "src" / "a.py").write_text("print(1)\n", encoding="utf-8")
    (src / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")

    buf_path = tmp_path / "w.tgz"
    pack_workspace_tar_gz(src, buf_path)
    import_tar_gz(buf_path.read_bytes(), label="proj", source=str(src))

    exported = export_active_tar_gz()
    assert isinstance(exported, (bytes, bytearray))
    assert len(exported) > 20

    dest = tmp_path / "pulled"
    written = extract_tar_gz_into(bytes(exported), dest)
    assert "src/a.py" in written
    assert "dirty.txt" in written
    assert (dest / "src" / "a.py").read_text(encoding="utf-8") == "print(1)\n"
    assert (dest / "dirty.txt").read_text(encoding="utf-8") == "uncommitted\n"

    # paths filter
    subset = export_active_tar_gz(paths=["src/a.py"])
    dest2 = tmp_path / "pulled2"
    written2 = extract_tar_gz_into(bytes(subset), dest2)
    assert written2 == ["src/a.py"]
    assert not (dest2 / "dirty.txt").exists()


# ---------------------------------------------------------------- secret floor


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        env={"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"},
    )


def _make_repo(root: Path) -> None:
    root.mkdir(parents=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "test")


def test_secret_named_files_never_upload(tmp_path: Path) -> None:
    """A non-git workspace must still never upload credentials.

    `hmd` is 10 nested repos under one non-repo root: `git ls-files` fails at
    the top level and the walk takes over. Secret names are the floor there.
    """
    src = tmp_path / "ws"
    (src / "src").mkdir(parents=True)
    (src / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    for secret in (".env", ".env.local", ".env.staging", ".credentials.local",
                   "id_ed25519", "server.pem", "tls.key"):
        (src / secret).write_text("SECRET\n", encoding="utf-8")
    (src / ".env.example").write_text("TEMPLATE=\n", encoding="utf-8")

    files = list_workspace_files(src)
    assert "src/app.py" in files
    assert ".env.example" in files, "templates are not secrets"
    for secret in (".env", ".env.local", ".env.staging", ".credentials.local",
                   "id_ed25519", "server.pem", "tls.key"):
        assert secret not in files, f"{secret} must not be uploaded"


def test_nested_repos_honor_their_own_gitignore(tmp_path: Path) -> None:
    """Each nested repo's .gitignore applies even though the parent is not a repo."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "README.md").write_text("top\n", encoding="utf-8")

    nested = ws / "app"
    _make_repo(nested)
    (nested / "keep.py").write_text("keep\n", encoding="utf-8")
    (nested / "ignore_me.txt").write_text("no\n", encoding="utf-8")
    (nested / ".gitignore").write_text("ignore_me.txt\n.env\n", encoding="utf-8")
    (nested / ".env").write_text("SECRET\n", encoding="utf-8")
    _git(nested, "add", "-A")

    files = list_workspace_files(ws)
    assert "README.md" in files
    assert "app/keep.py" in files, "nested repo contents must still upload"
    assert "app/ignore_me.txt" not in files, "nested .gitignore must be honored"
    assert "app/.env" not in files, "secret floor applies inside nested repos too"


def test_walk_captures_nested_repo_contents(tmp_path: Path) -> None:
    """Regression: git-treating nested repos as opaque dirs uploaded ~2 files."""
    ws = tmp_path / "ws"
    ws.mkdir()
    nested = ws / "app"
    _make_repo(nested)
    (nested / "src").mkdir()
    (nested / "src" / "mod.py").write_text("def f(): pass\n", encoding="utf-8")
    _git(nested, "add", "-A")

    files = list_workspace_files(ws)
    assert "app/src/mod.py" in files
    assert len(files) >= 1


def test_repo_root_still_uses_git_list(tmp_path: Path) -> None:
    """A plain single-repo workspace keeps honoring its .gitignore."""
    repo = tmp_path / "repo"
    _make_repo(repo)
    (repo / "keep.py").write_text("keep\n", encoding="utf-8")
    (repo / "secret.env").write_text("no\n", encoding="utf-8")
    (repo / ".gitignore").write_text("secret.env\n", encoding="utf-8")
    _git(repo, "add", "-A")

    files = list_workspace_files(repo)
    assert "keep.py" in files
    assert "secret.env" not in files
