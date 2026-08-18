"""Optional live smoke for ar-eval (skipped without engine)."""

from __future__ import annotations

from pathlib import Path

import pytest

from ar_eval.engine import health


def _engine_up() -> bool:
    try:
        health(timeout=2.0)
        return True
    except (RuntimeError, OSError, ConnectionError):
        return False


@pytest.mark.skipif(not _engine_up(), reason="autorefactor engine not reachable on :8765")
def test_engine_health_live() -> None:
    payload = health()
    assert payload is not None


@pytest.mark.skipif(not _engine_up(), reason="autorefactor engine not reachable on :8765")
def test_wave_skip_agents_live() -> None:
    from ar_eval.waves import run_waves

    results = run_waves(
        repo_root=Path(__file__).resolve().parents[1],
        max_episodes=1,
        episode_ids=["demo-alpha"],
        skip_agents=True,
        use_local_corpus=False,
        require_engine=True,
    )
    assert len(results) == 1
    assert results[0]["engine"]["ok"] is True
    assert results[0]["engine"]["sync"]["pack"]["file_count"] >= 1
