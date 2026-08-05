"""Deprecated mega-pipeline module — prefer ``coderazor.stages``."""

from __future__ import annotations

from coderazor.artifacts import load_artifacts
from coderazor.config import CoderazorConfig
from coderazor.models import AnalysisArtifacts
from coderazor.stages.analyze import build_provider, run_analyze
from coderazor.stages.plan import run_plan


def run_plan_only(config: CoderazorConfig) -> AnalysisArtifacts:
    return run_plan(config, require_harness=False)


__all__ = [
    "build_provider",
    "load_artifacts",
    "run_analyze",
    "run_plan_only",
]
