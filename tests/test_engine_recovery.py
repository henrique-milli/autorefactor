from __future__ import annotations

import inspect

from autorefactor.mcp import engine_recovery as er


def test_ensure_remote_engine_reflects_health(monkeypatch) -> None:
    monkeypatch.setattr(er, "remote_engine_healthy", lambda **kwargs: False)
    assert er.ensure_remote_engine() is False

    monkeypatch.setattr(er, "remote_engine_healthy", lambda **kwargs: True)
    assert er.ensure_remote_engine() is True


def test_engine_recovery_has_no_docker_dependency() -> None:
    source = inspect.getsource(er)
    assert "docker" not in source
    assert not hasattr(er, "subprocess")
