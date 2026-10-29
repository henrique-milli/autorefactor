from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from collections.abc import Callable
from importlib.metadata import entry_points

from autorefactor.config import STATIC_SENSOR_IDS
from autorefactor.domain import FOCUS_GOALS, AnalysisGoal, Finding, SensorReport
from autorefactor.sensors.base import AnalysisContext, Sensor, SensorRunResult


def discover_sensors() -> dict[str, Sensor]:
    """Load sensors from the ``autorefactor.sensors`` entry point group."""
    found: dict[str, Sensor] = {}
    try:
        eps = entry_points(group="autorefactor.sensors")
    except TypeError:  # pragma: no cover — py3.11+ uses group=
        eps = entry_points().select(group="autorefactor.sensors")  # type: ignore[attr-defined]
    for ep in eps:
        try:
            obj = ep.load()
            sensor = obj() if callable(obj) and not hasattr(obj, "id") else obj
            if callable(obj) and not hasattr(sensor, "id"):
                sensor = obj()
            sid = getattr(sensor, "id", None) or ep.name
            found[sid] = sensor
        except Exception:  # noqa: BLE001
            continue
    # Always include builtins if entry points missing (editable install edge cases)
    if not found:
        from autorefactor.sensors.builtins import builtin_sensors

        for s in builtin_sensors():
            found[s.id] = s
    else:
        # Merge builtins that may not be on the path yet during mid-refactor
        try:
            from autorefactor.sensors.builtins import builtin_sensors

            for s in builtin_sensors():
                found.setdefault(s.id, s)
        except Exception:  # noqa: BLE001
            pass
    return found


def enabled_sensor_ids(config_sensors: str, registry: dict[str, Sensor]) -> list[str]:
    raw = (config_sensors or "auto").strip()
    if raw in {"", "auto", "*"}:
        # Prefer explicit core list from config.enabled_sensors(); callers should pass that.
        from autorefactor.config import CORE_SENSORS

        requested = [p.strip() for p in CORE_SENSORS.split(",") if p.strip()]
    else:
        requested = [p.strip() for p in raw.split(",") if p.strip()]
    # Map legacy analyzer names
    aliases = {
        "builtin": "structure",
        "coverage": "coverage",
        "semgrep": "static.semgrep",
        "ruff": "static.ruff",
        "eslint": "static.eslint",
        "java_static": "static.java",
        "complexity": "complexity",
    }
    out: list[str] = []
    for name in requested:
        mapped = aliases.get(name, name)
        if mapped in registry:
            out.append(mapped)
        elif name in registry:
            out.append(name)
    return list(dict.fromkeys(out)) if out else sorted(registry.keys())


def run_sensors(
    ctx: AnalysisContext,
    *,
    concurrency: int = 8,
    on_progress: Callable[[str, int, int], None] | None = None,
) -> tuple[list[Finding], list[SensorReport]]:
    registry = discover_sensors()
    names = enabled_sensor_ids(ctx.config.enabled_sensors(), registry)
    allowed_goals = FOCUS_GOALS.get(ctx.focus, FOCUS_GOALS["balanced"])

    def _run_one(name: str) -> SensorRunResult:
        sensor = registry.get(name)
        if sensor is None:
            return SensorRunResult(
                [], SensorReport(sensor=name, status="skipped", detail="unknown sensor")
            )
        if sensor.goal not in allowed_goals:
            return SensorRunResult(
                [],
                SensorReport(
                    sensor=name, status="skipped", detail=f"focus={ctx.focus} excludes {sensor.goal.value}"
                ),
            )
        if not sensor.available(ctx):
            # Requested static sensors must not quietly skip
            if name in STATIC_SENSOR_IDS:
                return SensorRunResult(
                    [],
                    SensorReport(
                        sensor=name,
                        status="error",
                        detail="required tool not available on PATH (pip/brew install CLI, or use Docker image)",
                    ),
                )
            return SensorRunResult(
                [], SensorReport(sensor=name, status="skipped", detail="not available")
            )
        try:
            findings = sensor.collect(ctx)
            return SensorRunResult(
                findings,
                SensorReport(sensor=name, status="ran", finding_count=len(findings)),
            )
        except Exception as exc:  # noqa: BLE001
            return SensorRunResult(
                [],
                SensorReport(sensor=name, status="error", detail=str(exc)[:300]),
            )

    findings: list[Finding] = []
    reports: list[SensorReport] = []
    total = len(names)
    # Large workspaces: run sensors one-by-one (clearer progress, no GIL pile-up).
    if max(1, concurrency) == 1:
        for done_idx, name in enumerate(names, start=1):
            if on_progress is not None:
                on_progress(f"start:{name}", done_idx - 1, total)
            result = _run_one(name)
            findings.extend(result.findings)
            reports.append(result.report)
            if on_progress is not None:
                on_progress(result.report.sensor, done_idx, total)
    else:
        done = 0
        with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
            futs = {pool.submit(_run_one, n): n for n in names}
            for fut in as_completed(futs):
                result = fut.result()
                findings.extend(result.findings)
                reports.append(result.report)
                done += 1
                if on_progress is not None:
                    on_progress(result.report.sensor, done, total)

    return findings, reports
