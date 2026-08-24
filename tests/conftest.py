from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _proxy_inprocess_for_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unit tests monkeypatch remote helpers; avoid subprocess isolation there."""
    monkeypatch.setenv("AUTOREFACTOR_PROXY_INPROCESS", "1")
