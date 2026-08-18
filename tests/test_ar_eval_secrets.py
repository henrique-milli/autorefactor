"""Tests for secret file loading."""

from __future__ import annotations

import os
from pathlib import Path

from ar_eval.secrets import load_secret_files, resolve_cursor_api_key


def test_load_secret_files_does_not_override_env(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CURSOR_API_KEY", "from-env")
    f = tmp_path / "secrets.local.env"
    f.write_text("CURSOR_API_KEY=from-file\n", encoding="utf-8")
    load_secret_files(f, override=False)
    assert os.environ["CURSOR_API_KEY"] == "from-env"


def test_resolve_cursor_api_key_from_eval_secrets(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    eval_dir = tmp_path / "eval"
    eval_dir.mkdir()
    (eval_dir / "secrets.local.env").write_text(
        'CURSOR_API_KEY="cursor_test_key"\n',
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text("CURSOR_API_KEY=other\n", encoding="utf-8")
    key = resolve_cursor_api_key(repo_root=tmp_path, eval_dir=eval_dir)
    assert key == "cursor_test_key"


def test_resolve_falls_back_to_repo_dotenv(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    eval_dir = tmp_path / "eval"
    eval_dir.mkdir()
    (tmp_path / ".env").write_text("CURSOR_API_KEY=from-dotenv\n", encoding="utf-8")
    key = resolve_cursor_api_key(repo_root=tmp_path, eval_dir=eval_dir)
    assert key == "from-dotenv"
