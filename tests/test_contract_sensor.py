from __future__ import annotations

from pathlib import Path

from autorefactor.config import AutorefactorConfig
from autorefactor.sensors.base import AnalysisContext
from autorefactor.sensors.contract import ContractSensor
from autorefactor.stages.index import run_index

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "demo-workspace"


def test_contract_finds_merge_scores_ts_gap(tmp_path: Path) -> None:
    units = run_index(AutorefactorConfig(workspace=FIXTURES, out_dir=tmp_path, skip_llm=True))
    ctx = AnalysisContext(
        workspace=FIXTURES,
        repos=tuple(p for p in FIXTURES.iterdir() if p.is_dir()),
        units=tuple(units),
        config=AutorefactorConfig(workspace=FIXTURES, skip_llm=True),
        focus="balanced",
    )
    findings = ContractSensor().collect(ctx)
    assert any(
        "merge_scores" in (f.metadata.get("canonical") or "") or "merge_scores" in f.title.lower()
        for f in findings
    )
    naming = next((f.metadata.get("naming_map") for f in findings if f.metadata.get("naming_map")), None)
    assert naming and "snake_case" in naming and "camelCase" in naming
