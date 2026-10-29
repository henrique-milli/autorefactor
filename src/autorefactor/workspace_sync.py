from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import time
from pathlib import Path
from typing import BinaryIO

from autorefactor.paths import autorefactor_home

ACTIVE_NAME = "active"
META_NAME = ".autorefactor-sync.json"


def workspaces_root() -> Path:
    path = autorefactor_home() / "workspaces"
    path.mkdir(parents=True, exist_ok=True)
    return path


def active_workspace_dir() -> Path:
    return workspaces_root() / ACTIVE_NAME


def active_workspace_meta_path() -> Path:
    return active_workspace_dir() / META_NAME


def mounted_workspace_dir() -> Path:
    """Configured mount path (env) or Docker convention ``/workspace``."""
    env = (os.environ.get("AUTOREFACTOR_MCP_WORKSPACE") or "").strip()
    if env:
        return Path(env).expanduser()
    return Path("/workspace")


def _dir_nonempty(path: Path) -> bool:
    try:
        return path.is_dir() and any(path.iterdir())
    except OSError:
        return False


def _active_workspace_if_present() -> Path | None:
    active = active_workspace_dir()
    if active.exists() and any(p for p in active.iterdir() if p.name != META_NAME):
        return active.resolve()
    return None


def _active_has_sync_meta() -> bool:
    """True when synced/git active workspace exists with .autorefactor-sync.json."""
    if _active_workspace_if_present() is None:
        return False
    return active_workspace_meta_path().is_file()


def _read_active_sync_meta() -> dict:
    path = active_workspace_meta_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _sync_meta_for_path(path: Path) -> dict:
    """Sync label/source for a resolved workspace (not stale active meta)."""
    from autorefactor.paths import load_sync_meta, run_identity_for_workspace

    resolved = path.expanduser().resolve()
    meta = load_sync_meta(resolved) or {}
    if meta.get("label"):
        return meta
    ident = run_identity_for_workspace(resolved)
    return {
        "label": ident.get("label"),
        "source": ident.get("source") or str(resolved),
    }


def _cwd_prefers_over_active_sync() -> bool:
    """True when cwd should win over stale active sync (eval subjects, local sync meta)."""
    cwd = Path.cwd()
    if not _dir_nonempty(cwd):
        return False
    if not _active_has_sync_meta():
        return False
    resolved = cwd.resolve()
    parts = resolved.parts
    if "eval" in parts and "subjects" in parts:
        idx = parts.index("subjects")
        if idx > 0 and parts[idx - 1] == "eval" and idx + 1 < len(parts):
            return True
    from autorefactor.paths import load_sync_meta, run_identity_for_workspace

    if not load_sync_meta(resolved):
        return False
    active_meta = _read_active_sync_meta()
    cwd_label = str(run_identity_for_workspace(resolved).get("label") or "").strip()
    active_label = str(active_meta.get("label") or "").strip()
    return bool(active_label and cwd_label and active_label != cwd_label)


def resolve_workspace_info(workspace: str | None = None) -> dict:
    """Resolve workspace and return path + how it was chosen (for MCP echoes)."""
    path = resolve_analyze_workspace(workspace)
    meta = _sync_meta_for_path(path)
    via = "explicit"
    raw = (workspace or "").strip()
    prefer_active = (os.environ.get("AUTOREFACTOR_MCP_PREFER_ACTIVE") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    active = _active_workspace_if_present()
    if raw in {"active", "synced", "remote"}:
        via = "active"
    elif raw in {"mounted"}:
        via = "mounted"
    elif raw in {"", "default", "."}:
        if active is not None and path == active and (prefer_active or _active_has_sync_meta()):
            via = "active_sync" if _active_has_sync_meta() else "active"
        else:
            env = (os.environ.get("AUTOREFACTOR_MCP_WORKSPACE") or "").strip()
            if env and path == Path(env).expanduser().resolve():
                via = "env"
            elif path == Path.cwd().resolve():
                via = "cwd"
            elif path == Path("/workspace").resolve():
                via = "docker_mount"
            elif active is not None and path == active:
                via = "active"
            else:
                via = "default"
    elif active is not None and path == active:
        via = "active"
    return {
        "path": path,
        "workspace": str(path),
        "resolved_via": via,
        "sync_source": meta.get("source"),
        "sync_label": meta.get("label"),
    }


def resolve_analyze_workspace(workspace: str | None) -> Path:
    """Map MCP workspace arg to a filesystem path.

    Default / empty / ``.`` / ``default``:

    1. Synced ``active`` when it has ``.autorefactor-sync.json`` (or ``PREFER_ACTIVE=1``)
    2. ``AUTOREFACTOR_MCP_WORKSPACE`` if set and non-empty
    3. process cwd if non-empty
    4. ``/workspace`` if non-empty (Docker bind mount)
    5. synced/git ``active`` copy without meta

    ``mounted`` → mount/cwd/docker only (never auto-prefer active).
    ``active`` / ``synced`` → last uploaded or git-prepared workspace.
    """
    raw = (workspace or "").strip()
    prefer_active = (os.environ.get("AUTOREFACTOR_MCP_PREFER_ACTIVE") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if raw in {"", "default", "."}:
        cwd = Path.cwd()
        # Eval subjects / any cwd whose identity differs from stale active sync meta.
        if _dir_nonempty(cwd) and _cwd_prefers_over_active_sync():
            return cwd.resolve()
        # Synced active is authoritative for MCP analyze once a sync/prepare happened.
        if prefer_active or _active_has_sync_meta():
            active = _active_workspace_if_present()
            if active is not None:
                return active
        env = (os.environ.get("AUTOREFACTOR_MCP_WORKSPACE") or "").strip()
        if env:
            mounted = Path(env).expanduser()
            if _dir_nonempty(mounted):
                return mounted.resolve()
        cwd = Path.cwd()
        if _dir_nonempty(cwd):
            return cwd.resolve()
        docker_mount = Path("/workspace")
        if _dir_nonempty(docker_mount):
            return docker_mount.resolve()
        active = _active_workspace_if_present()
        if active is not None:
            return active
        raise RuntimeError(
            "No workspace found. Set AUTOREFACTOR_MCP_WORKSPACE, pass an absolute path, "
            "run `prepare_workspace` with a git_url, or `autorefactor sync` then workspace=active."
        )
    if raw == "mounted":
        env = (os.environ.get("AUTOREFACTOR_MCP_WORKSPACE") or "").strip()
        if env:
            mounted = Path(env).expanduser()
            if _dir_nonempty(mounted):
                return mounted.resolve()
        cwd = Path.cwd()
        if _dir_nonempty(cwd):
            return cwd.resolve()
        docker_mount = Path("/workspace")
        if _dir_nonempty(docker_mount):
            return docker_mount.resolve()
        raise RuntimeError(
            "No mounted workspace found. Set AUTOREFACTOR_MCP_WORKSPACE or bind /workspace."
        )
    if raw in {"active", "synced", "remote"}:
        path = _active_workspace_if_present()
        if path is None:
            raise RuntimeError(
                "No prepared workspace. Call prepare_workspace(git_url=…) or "
                "`autorefactor sync -w <project>`, then analyze with workspace=active."
            )
        return path
    return Path(raw).expanduser().resolve()


def _git_files(root: Path) -> list[str] | None:
    """Tracked + untracked non-ignored paths relative to root (working tree content)."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z", "-c", "-o", "--exclude-standard"],
            check=False,
            capture_output=True,
        )
    except FileNotFoundError:
        return None
    if proc.returncode != 0:
        return None
    items = [p for p in proc.stdout.decode("utf-8", errors="replace").split("\0") if p]
    return items


# Secret-bearing filenames are never uploaded, regardless of git state or rules.
# A workspace that is not a git repo has no .gitignore to honor, so these are the
# floor. `hmd` is such a workspace: 10 nested repos under one non-repo root.
_SECRET_NAME_RE = re.compile(
    r"^\.env(\..+)?$"
    r"|\.credentials(\.local)?$"
    r"|.*\.(pem|key|p12|pfx)$"
    r"|^id_(rsa|ed25519|ecdsa|dsa).*"
)

# Placeholder templates are documentation, not secrets. Allow these by exact
# name so a real `.env.local` is still blocked while `.env.example` ships.
_SECRET_NAME_ALLOW = frozenset(
    {
        ".env.example",
        ".env.sample",
        ".env.template",
        ".env.test",
        ".env.local.example",
        ".credentials.local.example",
    }
)


def _is_secret_name(name: str) -> bool:
    if name in _SECRET_NAME_ALLOW:
        return False
    return bool(_SECRET_NAME_RE.match(name))


def _is_git_worktree(path: Path) -> bool:
    """A repo root: .git dir (normal) or .git file (worktree / submodule)."""
    try:
        return (path / ".git").exists()
    except OSError:
        return False


_SKIP_DIR_NAMES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".idea",
        ".vscode",
        "node_modules",
        "target",
        "build",
        "dist",
        ".venv",
        "venv",
        "__pycache__",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
        ".autorefactor-data",
        "out",
    }
)


def _walk_files(root: Path) -> list[str]:
    """Walk files under root, following directory symlinks (for eval symlink mode).

    A workspace that is not a git repo still contains nested repos (e.g. `hmd`
    is 10 sibling clones under one non-repo root). Each nested repo has its own
    `.gitignore`, so on entering one we delegate to `git ls-files` and honor
    that repo's rules instead of raw-walking it. Secret-named files are dropped
    everywhere, git or not.
    """
    out: list[str] = []
    root = root.resolve()

    def take(repo_root: Path, rel_prefix: str) -> None:
        """Walk one directory tree, pruning skipped dirs and secret names."""
        for dirpath, dirnames, filenames in os.walk(repo_root, followlinks=True):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIR_NAMES]
            base = Path(dirpath)
            for name in filenames:
                if _is_secret_name(name):
                    continue
                path = base / name
                if not path.is_file():
                    continue
                try:
                    rel = path.relative_to(repo_root)
                except ValueError:
                    continue
                if any(part in _SKIP_DIR_NAMES for part in rel.parts):
                    continue
                out.append((rel_prefix + str(rel)).replace("\\", "/"))

    # Root may itself be a git worktree: honor its .gitignore via git ls-files.
    if _is_git_worktree(root):
        listed = _git_files(root)
        if listed is not None:
            return [p for p in listed if not _is_secret_name(Path(p).name)]
        # git unavailable: fall through to walking, with the secret floor.

    for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIR_NAMES]
        base = Path(dirpath)

        # Nested repos delegate to their own gitignore rather than raw walking.
        for d in list(dirnames):
            sub = base / d
            if not _is_git_worktree(sub):
                continue
            dirnames.remove(d)  # do not walk into it twice
            rel_prefix = str(sub.relative_to(root)).replace("\\", "/") + "/"
            listed = _git_files(sub)
            if listed is not None:
                out.extend(
                    rel_prefix + p
                    for p in listed
                    if not _is_secret_name(Path(p).name)
                )
            else:
                take(sub, rel_prefix)

        for name in filenames:
            if _is_secret_name(name):
                continue
            path = base / name
            if not path.is_file():
                continue
            try:
                rel = path.relative_to(root)
            except ValueError:
                continue
            if any(part in _SKIP_DIR_NAMES for part in rel.parts):
                continue
            out.append(str(rel).replace("\\", "/"))

    return out


FORCE_WALK_MARKER = ".ar_eval_force_walk"
_COVERAGE_REPORT_NAMES = frozenset({"jacoco.xml", "jacocotestreport.xml"})


def _coverage_report_relpaths(root: Path) -> list[str]:
    """Jacoco XML under target/build even when those dirs are otherwise skipped."""
    out: list[str] = []
    for pattern in ("**/jacoco.xml", "**/jacocoTestReport.xml"):
        for path in root.glob(pattern):
            if not path.is_file():
                continue
            try:
                rel = str(path.relative_to(root)).replace("\\", "/")
            except ValueError:
                continue
            if path.name.lower() in _COVERAGE_REPORT_NAMES:
                out.append(rel)
    return out


def list_workspace_files(root: Path) -> list[str]:
    root = root.resolve()
    force_walk = (root / FORCE_WALK_MARKER).is_file()
    files = None if force_walk else _git_files(root)
    if files is None:
        files = _walk_files(root)
    files = list(files) + _coverage_report_relpaths(root)
    # Drop sync meta if present
    return sorted(
        {
            f
            for f in files
            if f
            and f != META_NAME
            and f != FORCE_WALK_MARKER
            and not f.startswith(".git/")
        }
    )


def pack_workspace_tar_gz(root: Path, dest: BinaryIO | Path) -> dict:
    """Pack working-tree files (incl. uncommitted) into a gzipped tar."""
    root = root.resolve()
    if not root.is_dir():
        raise RuntimeError(f"Not a directory: {root}")
    files = list_workspace_files(root)
    if isinstance(dest, Path):
        dest.parent.mkdir(parents=True, exist_ok=True)
        fh: BinaryIO = dest.open("wb")
        close = True
    else:
        fh = dest
        close = False
    try:
        with tarfile.open(fileobj=fh, mode="w:gz", format=tarfile.PAX_FORMAT) as tar:
            for rel in files:
                path = root / rel
                if not path.is_file():
                    continue
                tar.add(path, arcname=rel)
    finally:
        if close:
            fh.close()
    return {"root": str(root), "file_count": len(files), "files_sample": files[:20]}


def _swap_staging_to_active(staging: Path, meta: dict) -> dict:
    active = active_workspace_dir()
    if active.exists():
        bak = workspaces_root() / f".old-{int(time.time())}"
        active.rename(bak)
        shutil.rmtree(bak, ignore_errors=True)
    staging.rename(active)
    file_count = sum(1 for p in active.rglob("*") if p.is_file() and p.name != META_NAME)
    payload = {
        "ok": True,
        "synced_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "file_count": file_count,
        "root": str(active.resolve()),
        **meta,
    }
    active_workspace_meta_path().write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def import_tar_gz(
    data: bytes,
    *,
    label: str | None = None,
    source: str | None = None,
) -> dict:
    """Replace the active synced workspace with the contents of a tar.gz."""
    staging = workspaces_root() / f".staging-{int(time.time())}"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            # Safety: reject absolute / parent paths
            for member in tar.getmembers():
                name = member.name.replace("\\", "/")
                if name.startswith("/") or ".." in Path(name).parts:
                    raise RuntimeError(f"Unsafe tar member: {member.name}")
            try:
                tar.extractall(staging, filter="data")  # type: ignore[call-arg]
            except TypeError:
                tar.extractall(staging)

        return _swap_staging_to_active(
            staging,
            {"label": label or ACTIVE_NAME, "source": source, "kind": "sync"},
        )
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _safe_tar_members(tar: tarfile.TarFile) -> list[tarfile.TarInfo]:
    members: list[tarfile.TarInfo] = []
    for member in tar.getmembers():
        name = member.name.replace("\\", "/")
        if name.startswith("/") or ".." in Path(name).parts:
            raise RuntimeError(f"Unsafe tar member: {member.name}")
        if Path(name).name == META_NAME:
            continue
        members.append(member)
    return members


def export_active_tar_gz(
    dest: BinaryIO | Path | None = None,
    *,
    paths: list[str] | None = None,
) -> bytes | dict:
    """Pack the active workspace into a gzipped tar (excludes sync meta).

    When ``dest`` is None, returns the raw bytes. Otherwise writes to ``dest``
    and returns pack metadata (same shape as ``pack_workspace_tar_gz``).
    """
    active = _active_workspace_if_present()
    if active is None:
        raise RuntimeError("No prepared workspace to export")

    if paths:
        wanted = {p.replace("\\", "/").lstrip("./") for p in paths if p}
        # Single-root workspaces are indexed under the workspace dir name (repo="active"),
        # so touched paths arrive as "active/src/…" while files are root-relative "src/…".
        prefix = f"{active.name}/"
        wanted = wanted | {
            p[len(prefix) :] for p in list(wanted) if p.startswith(prefix)
        }
        files = sorted(f for f in list_workspace_files(active) if f in wanted)
        if not files:
            # Fall back to walking requested paths that exist on disk
            files = []
            for rel in sorted(wanted):
                path = active / rel
                if path.is_file():
                    files.append(rel)
    else:
        files = list_workspace_files(active)

    buf: BinaryIO
    close = False
    return_bytes = dest is None
    if return_bytes:
        buf = io.BytesIO()
    elif isinstance(dest, Path):
        dest.parent.mkdir(parents=True, exist_ok=True)
        buf = dest.open("wb")
        close = True
    else:
        assert dest is not None
        buf = dest

    try:
        with tarfile.open(fileobj=buf, mode="w:gz", format=tarfile.PAX_FORMAT) as tar:
            for rel in files:
                path = active / rel
                if not path.is_file():
                    continue
                tar.add(path, arcname=rel)
        meta = {"root": str(active), "file_count": len(files), "files_sample": files[:20]}
        if return_bytes:
            assert isinstance(buf, io.BytesIO)
            return buf.getvalue()
        return meta
    finally:
        if close:
            buf.close()


def extract_tar_gz_into(data: bytes, dest: Path) -> list[str]:
    """Extract a gzipped tar into ``dest``, returning relative paths written."""
    dest = dest.resolve()
    dest.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        members = _safe_tar_members(tar)
        for member in members:
            if not member.isfile():
                continue
            name = member.name.replace("\\", "/").lstrip("./")
            target = (dest / name).resolve()
            if not str(target).startswith(str(dest) + os.sep) and target != dest:
                raise RuntimeError(f"Unsafe tar member escapes dest: {member.name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            src = tar.extractfile(member)
            if src is None:
                continue
            with src, target.open("wb") as out:
                shutil.copyfileobj(src, out)
            written.append(name)
    return written


def validate_git_url(git_url: str) -> str:
    """Return cleaned git URL or raise if unsafe / unsupported."""
    url = (git_url or "").strip()
    if not url:
        raise RuntimeError("git_url is required")
    lowered = url.lower()
    if lowered.startswith("file:") or url.startswith("/") or url.startswith("."):
        raise RuntimeError("Local paths are not allowed for prepare_workspace; use https/ssh git URL")
    if ".." in url.split("://", 1)[-1].split("/")[0:2]:
        pass  # host only; path .. checked below
    if any(p == ".." for p in Path(url.replace("\\", "/")).parts):
        raise RuntimeError("Invalid git_url")
    # Reject embedded credentials — agents must use AUTOREFACTOR_GIT_TOKEN on the server.
    if lowered.startswith(("https://", "http://")):
        rest = url.split("://", 1)[1]
        hostport = rest.split("/", 1)[0]
        if "@" in hostport:
            raise RuntimeError(
                "Do not put tokens or credentials in git_url. "
                "Set AUTOREFACTOR_GIT_TOKEN (or GITHUB_TOKEN) on the MCP server instead. "
                "For local projects prefer analyze (auto-sync) or sync_workspace."
            )
    if lowered.startswith(("https://", "http://", "ssh://")) or url.startswith("git@"):
        return url
    raise RuntimeError("git_url must be https://, http://, ssh://, or git@…")


def _authed_https_url(git_url: str) -> str:
    token = (os.environ.get("AUTOREFACTOR_GIT_TOKEN") or os.environ.get("GITHUB_TOKEN") or "").strip()
    if not token or not git_url.lower().startswith("https://"):
        return git_url
    rest = git_url[len("https://") :]
    if "@" in rest.split("/", 1)[0]:
        return git_url
    from urllib.parse import quote

    return f"https://x-access-token:{quote(token, safe='')}@{rest}"


def _clone_into(dest: Path, git_url: str, *, ref: str | None, depth: int) -> str:
    """Clone ``git_url`` into ``dest`` (must not exist). Returns validated URL."""
    url = validate_git_url(git_url)
    clone_url = _authed_https_url(url)
    cmd = ["git", "clone", "--depth", str(max(1, depth))]
    if ref:
        cmd.extend(["--branch", ref])
    cmd.extend([clone_url, str(dest)])
    try:
        proc = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=600)
    except FileNotFoundError as exc:
        raise RuntimeError("git is not installed on this MCP server") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"git clone timed out for {url}") from exc
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "clone failed").strip()[:500]
        raise RuntimeError(f"git clone failed ({url}): {err}")
    git_dir = dest / ".git"
    if git_dir.exists():
        shutil.rmtree(git_dir, ignore_errors=True)
    return url


def _repo_dir_name(git_url: str) -> str:
    stem = Path(git_url.rstrip("/")).stem
    if stem.endswith(".git"):
        stem = stem[:-4]
    safe = "".join(c if c.isalnum() or c in "._-" else "-" for c in stem).strip("-")
    return safe or "repo"


def clone_git_workspace(
    git_url: str,
    *,
    ref: str | None = None,
    depth: int = 1,
) -> dict:
    """Shallow-clone a remote git repo into the active workspace."""
    staging = workspaces_root() / f".staging-git-{int(time.time())}"
    if staging.exists():
        shutil.rmtree(staging)
    try:
        url = _clone_into(staging, git_url, ref=ref, depth=depth)
        return _swap_staging_to_active(
            staging,
            {
                "label": Path(url.rstrip("/")).stem or ACTIVE_NAME,
                "source": "git",
                "kind": "git",
                "git_url": url,
                "git_urls": [url],
                "ref": ref,
                "repos": 1,
            },
        )
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def clone_git_workspaces(
    git_urls: list[str],
    *,
    ref: str | None = None,
    depth: int = 1,
) -> dict:
    """Shallow-clone multiple repos as siblings under the active workspace.

    Enables remote cross-repo analyze without a bind mount: prepare several
    ``git_url``s, then ``analyze(workspace=active)``.
    """
    urls = [u.strip() for u in git_urls if (u or "").strip()]
    if not urls:
        raise RuntimeError("git_urls must contain at least one repository URL")
    if len(urls) == 1:
        return clone_git_workspace(urls[0], ref=ref, depth=depth)

    staging = workspaces_root() / f".staging-git-multi-{int(time.time())}"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True, exist_ok=True)
    cloned: list[str] = []
    try:
        used_names: set[str] = set()
        for raw in urls:
            name = _repo_dir_name(raw)
            base = name
            n = 2
            while name in used_names:
                name = f"{base}-{n}"
                n += 1
            used_names.add(name)
            url = _clone_into(staging / name, raw, ref=ref, depth=depth)
            cloned.append(url)
        return _swap_staging_to_active(
            staging,
            {
                "label": f"multi-{len(cloned)}",
                "source": "git",
                "kind": "git_multi",
                "git_urls": cloned,
                "ref": ref,
                "repos": len(cloned),
            },
        )
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def workspace_status() -> dict:
    active = active_workspace_dir()
    meta_path = active_workspace_meta_path()
    if not active.exists():
        return {"ok": True, "synced": False, "root": str(active)}
    meta = {}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            meta = {}
    file_count = sum(1 for p in active.rglob("*") if p.is_file() and p.name != META_NAME)
    return {
        "ok": True,
        "synced": file_count > 0,
        "root": str(active.resolve()),
        "file_count": file_count,
        **meta,
    }
