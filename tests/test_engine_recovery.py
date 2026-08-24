from __future__ import annotations

from autorefactor.mcp import engine_recovery as er


def test_restart_remote_engine_invokes_docker(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):  # noqa: ANN001
        calls.append(list(cmd))
        class R:
            returncode = 0
            stdout = "autorefactor\n"
            stderr = ""

        return R()

    monkeypatch.setattr(er.subprocess, "run", fake_run)
    monkeypatch.setattr(er, "remote_engine_healthy", lambda **kwargs: True)

    out = er.restart_remote_engine(wait_s=0)
    assert out["ok"] is True
    assert calls == [["docker", "restart", "autorefactor"]]
