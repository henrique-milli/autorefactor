from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ArtifactLayout:
    """Stage-owned directories under ``out_dir``."""

    out_dir: Path

    @property
    def cache_dir(self) -> Path:
        return self.out_dir / "cache"

    @property
    def index_dir(self) -> Path:
        return self.out_dir / "index"

    @property
    def analyze_dir(self) -> Path:
        return self.out_dir / "analyze"

    @property
    def document_dir(self) -> Path:
        return self.out_dir / "document"

    @property
    def test_dir(self) -> Path:
        return self.out_dir / "test"

    @property
    def plan_dir(self) -> Path:
        return self.out_dir / "plan"

    @property
    def db_path(self) -> Path:
        return self.cache_dir / "index.db"

    def ensure(self) -> ArtifactLayout:
        for path in (
            self.cache_dir,
            self.index_dir,
            self.analyze_dir,
            self.document_dir,
            self.test_dir,
            self.plan_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)
        return self


def layout_for(out_dir: Path) -> ArtifactLayout:
    return ArtifactLayout(out_dir=out_dir.resolve()).ensure()
