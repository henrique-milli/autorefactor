from __future__ import annotations

from pathlib import Path

from autorefactor.models import AnalysisArtifacts, CodeUnit, ConfirmedFinding, TestPlan
from autorefactor.testing.gaps import find_test_gaps
from autorefactor.testing.inventory import inventory_test_files


def build_test_plan(
    *,
    repos: list[Path],
    findings: list[ConfirmedFinding],
    units: list[CodeUnit],
    recommended_cmd: str | None = None,
) -> TestPlan:
    existing = inventory_test_files(repos)
    gaps = find_test_gaps(findings, units, existing)
    summary = (
        f"Found {len(existing)} existing test file(s); "
        f"{len(gaps)} characterization gap(s) on confirmed refactor targets."
    )
    return TestPlan(
        summary=summary,
        gaps=gaps,
        existing_test_files=existing,
        recommended_cmd=recommended_cmd,
    )


def build_test_plan_from_artifacts(
    artifacts: AnalysisArtifacts,
    repos: list[Path],
    recommended_cmd: str | None = None,
) -> TestPlan:
    return build_test_plan(
        repos=repos,
        findings=artifacts.findings,
        units=artifacts.units,
        recommended_cmd=recommended_cmd,
    )
