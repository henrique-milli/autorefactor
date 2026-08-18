from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from autorefactor.models import (
    AnalysisArtifacts,
    CodeUnit,
    ConfirmedFinding,
    RefactorPlan,
    RunManifest,
    TestHarnessResult,
    TestPlan,
)


class IndexStore:
    """SQLite-backed persistence for units, flags, findings, and plans."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS kv (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS descriptions (
                unit_id TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                description TEXT NOT NULL,
                tags TEXT NOT NULL,
                PRIMARY KEY (unit_id, content_hash)
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS embeddings (
                content_hash TEXT NOT NULL,
                embed_model TEXT NOT NULL,
                dims INTEGER NOT NULL,
                vector TEXT NOT NULL,
                PRIMARY KEY (content_hash, embed_model)
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS extract_cache (
                repo TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                units_json TEXT NOT NULL,
                PRIMARY KEY (repo, fingerprint)
            )
            """
        )
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def get_description(self, unit_id: str, content_hash: str) -> tuple[str, list[str]] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT description, tags FROM descriptions WHERE unit_id=? AND content_hash=?",
                (unit_id, content_hash),
            ).fetchone()
        if not row:
            return None
        return row[0], json.loads(row[1])

    def put_description(self, unit_id: str, content_hash: str, description: str, tags: list[str]) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO descriptions (unit_id, content_hash, description, tags)
                VALUES (?, ?, ?, ?)
                """,
                (unit_id, content_hash, description, json.dumps(tags)),
            )
            self._conn.commit()

    def get_embedding(self, content_hash: str, embed_model: str) -> list[float] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT vector FROM embeddings WHERE content_hash=? AND embed_model=?",
                (content_hash, embed_model),
            ).fetchone()
        if not row:
            return None
        try:
            data = json.loads(row[0])
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, list) else None

    def put_embedding(self, content_hash: str, embed_model: str, vector: list[float]) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO embeddings (content_hash, embed_model, dims, vector)
                VALUES (?, ?, ?, ?)
                """,
                (content_hash, embed_model, len(vector), json.dumps(vector)),
            )
            self._conn.commit()

    def get_extract_cache(self, repo: str, fingerprint: str) -> list[dict] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT units_json FROM extract_cache WHERE repo=? AND fingerprint=?",
                (repo, fingerprint),
            ).fetchone()
        if not row:
            return None
        try:
            data = json.loads(row[0])
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, list) else None

    def put_extract_cache(self, repo: str, fingerprint: str, units_payload: list[dict]) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO extract_cache (repo, fingerprint, units_json)
                VALUES (?, ?, ?)
                """,
                (repo, fingerprint, json.dumps(units_payload)),
            )
            self._conn.commit()

    def save_artifacts(self, artifacts: AnalysisArtifacts) -> None:
        payload = {
            "units": [u.model_dump(mode="json") for u in artifacts.units],
            "findings": [f.model_dump(mode="json") for f in artifacts.findings],
            "confirmed": [f.model_dump(mode="json") for f in artifacts.confirmed],
            "flags": [f.model_dump(mode="json") for f in artifacts.findings],  # compat
            "plan": artifacts.plan.model_dump(mode="json") if artifacts.plan else None,
            "test_plan": artifacts.test_plan.model_dump(mode="json") if artifacts.test_plan else None,
            "test_harness": (
                artifacts.test_harness.model_dump(mode="json") if artifacts.test_harness else None
            ),
            "manifest": artifacts.manifest.model_dump(mode="json") if artifacts.manifest else None,
            "document_report": (
                artifacts.document_report.model_dump(mode="json")
                if artifacts.document_report
                else None
            ),
            "coverage": artifacts.coverage.model_dump(mode="json") if artifacts.coverage else None,
            "sensor_reports": [n.model_dump(mode="json") for n in artifacts.sensor_reports],
            "analyzer_notes": [n.model_dump(mode="json") for n in artifacts.sensor_reports],
        }
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)",
                ("artifacts", json.dumps(payload)),
            )
            self._conn.commit()

    def load_artifacts(self) -> AnalysisArtifacts | None:
        from autorefactor.domain import CoverageSnapshot, DocumentReport, Finding, SensorReport

        with self._lock:
            row = self._conn.execute("SELECT value FROM kv WHERE key=?", ("artifacts",)).fetchone()
        if not row:
            return None
        data = json.loads(row[0])
        raw_findings = data.get("findings") or data.get("flags") or []
        # Detect ConfirmedFinding vs Finding by shape
        evidence: list[Finding] = []
        confirmed: list[ConfirmedFinding] = []
        for item in raw_findings:
            if isinstance(item, dict) and "finding" in item:
                confirmed.append(ConfirmedFinding.model_validate(item))
            elif isinstance(item, dict) and "goal" in item:
                evidence.append(Finding.model_validate(item))
            elif isinstance(item, dict):
                try:
                    evidence.append(Finding.model_validate(item))
                except Exception:  # noqa: BLE001
                    continue
        for item in data.get("confirmed") or []:
            confirmed.append(ConfirmedFinding.model_validate(item))
        notes_raw = data.get("sensor_reports") or data.get("analyzer_notes") or []
        return AnalysisArtifacts(
            units=[CodeUnit.model_validate(u) for u in data.get("units", [])],
            findings=evidence,
            confirmed=confirmed,
            plan=RefactorPlan.model_validate(data["plan"]) if data.get("plan") else None,
            test_plan=TestPlan.model_validate(data["test_plan"]) if data.get("test_plan") else None,
            test_harness=(
                TestHarnessResult.model_validate(data["test_harness"])
                if data.get("test_harness")
                else None
            ),
            manifest=RunManifest.model_validate(data["manifest"]) if data.get("manifest") else None,
            document_report=(
                DocumentReport.model_validate(data["document_report"])
                if data.get("document_report")
                else None
            ),
            coverage=(
                CoverageSnapshot.model_validate(data["coverage"]) if data.get("coverage") else None
            ),
            sensor_reports=[SensorReport.model_validate(n) for n in notes_raw],
        )


def write_jsonl_units(path: Path, units: list[CodeUnit]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for unit in units:
            dump = unit.model_dump(mode="json")
            if dump.get("embedding"):
                dump["embedding"] = f"<{len(unit.embedding or [])} dims>"
            fh.write(json.dumps(dump) + "\n")
