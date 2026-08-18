from __future__ import annotations

import json
from pathlib import Path

from autorefactor.paths import autorefactor_home, default_run_dir, resolve_out_dir, workspace_run_slug


def test_workspace_run_slug_stable(tmp_path: Path) -> None:
    ws = tmp_path / "my-service"
    ws.mkdir()
    a = workspace_run_slug(ws)
    b = workspace_run_slug(ws)
    assert a == b
    assert a.startswith("my-service-")
    assert len(a.split("-")[-1]) == 10


def test_workspace_run_slug_prefers_sync_label(tmp_path: Path) -> None:
    from autorefactor.paths import SYNC_META_NAME, run_identity_for_workspace

    ws = tmp_path / "active"
    ws.mkdir()
    (ws / SYNC_META_NAME).write_text(
        json.dumps(
            {
                "label": "billing-service",
                "source": "/home/dev/Git/monorepo/billing-service",
                "kind": "sync",
            }
        ),
        encoding="utf-8",
    )
    ident = run_identity_for_workspace(ws)
    assert ident["label"] == "billing-service"
    assert ident["group"] == "monorepo"
    assert ident["slug"].startswith("billing-service-")
    assert workspace_run_slug(ws) == ident["slug"]


def test_default_run_dir_under_home(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    ws = tmp_path / "proj"
    ws.mkdir()
    run = default_run_dir(ws)
    assert run == home / ".autorefactor" / "runs" / workspace_run_slug(ws)
    assert run.is_dir()
    assert autorefactor_home() == home / ".autorefactor"


def test_resolve_out_dir_explicit_wins(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    ws = tmp_path / "proj"
    ws.mkdir()
    custom = tmp_path / "custom-out"
    assert resolve_out_dir(ws, custom) == custom
    assert resolve_out_dir(ws, None) == default_run_dir(ws)


def test_resolve_out_dir_prefers_eval_episode_run(tmp_path: Path, monkeypatch) -> None:
    """Eval subject cwd binds to janus-bonus-* run, not autorefactor-* default."""
    from autorefactor.paths import load_run_identity, write_run_identity

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    eval_ws = tmp_path / "eval" / "subjects" / "janus-bonus"
    eval_ws.mkdir(parents=True)
    (eval_ws / ".ar_eval_snapshot.json").write_text("{}", encoding="utf-8")

    runs = home / ".autorefactor" / "runs"
    auto_run = runs / "autorefactor-9b7384ecf0"
    episode_run = runs / "janus-bonus-0c874e7322"
    auto_run.mkdir(parents=True)
    episode_run.mkdir(parents=True)
    write_run_identity(auto_run, tmp_path / "autorefactor")
    write_run_identity(episode_run, eval_ws)
    assert load_run_identity(episode_run)["label"] == "janus-bonus"

    resolved = resolve_out_dir(eval_ws, None)
    assert resolved.name.startswith("janus-bonus-")
    assert resolved != auto_run


def test_resolve_out_dir_prefers_populated_run(tmp_path: Path, monkeypatch) -> None:
    from autorefactor.changesets.models import ChangesetStatus
    from autorefactor.paths import SYNC_META_NAME, write_run_identity

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    ws = tmp_path / "active"
    ws.mkdir()
    (ws / SYNC_META_NAME).write_text(
        json.dumps(
            {
                "label": "janus-bonus",
                "source": "/home/dev/Git/janus-bonus",
                "kind": "sync",
            }
        ),
        encoding="utf-8",
    )

    runs = home / ".autorefactor" / "runs"
    empty_run = runs / "janus-bonus-empty0001"
    populated_run = runs / "janus-bonus-populated1"
    empty_run.mkdir(parents=True)
    populated_run.mkdir(parents=True)
    write_run_identity(empty_run, ws)
    write_run_identity(populated_run, ws)

    queue = populated_run / "queue" / "changesets.json"
    queue.parent.mkdir(parents=True, exist_ok=True)
    queue.write_text(
        json.dumps(
            {
                "changesets": [
                    {
                        "id": "cs1",
                        "title": "Split",
                        "action": "split",
                        "brief": "b",
                        "rationale": "r",
                        "confidence": 0.99,
                        "targets": [],
                        "status": ChangesetStatus.PENDING.value,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    resolved = resolve_out_dir(ws, None)
    assert resolved == populated_run


def test_run_label_does_not_match_generic_ar_eval_source(tmp_path: Path, monkeypatch) -> None:
    from autorefactor.paths import SYNC_META_NAME, _run_label_matches, write_run_identity

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    retest = tmp_path / "active"
    retest.mkdir()
    (retest / SYNC_META_NAME).write_text(
        json.dumps({"label": "loop-retest-janus-bonus", "source": "ar_eval", "kind": "sync"}),
        encoding="utf-8",
    )
    runs = home / ".autorefactor" / "runs"
    old = runs / "janus-bonus-3ad6a7ba55"
    old.mkdir(parents=True)
    (old / "run_identity.json").write_text(
        json.dumps({"label": "janus-bonus", "source": "ar_eval", "slug": old.name}),
        encoding="utf-8",
    )
    assert _run_label_matches(old, retest) is False
    write_run_identity(old, retest)
    # After rewriting identity to the retest workspace, labels match.
    assert _run_label_matches(old, retest) is True
