"""Characterization tests for `alpha.util.normalize_user_id` (coderazor ensure-tests)."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

# Repo root (parent of tests/)
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

mod = importlib.import_module("alpha.util")


def test_alpha_util_normalize_user_id_is_importable():
    assert mod is not None


def test_alpha_util_normalize_user_id_symbol_exists():
    assert hasattr(mod, "normalize_user_id")
