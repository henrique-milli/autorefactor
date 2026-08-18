"""Unit tests for ar_eval corpus merge + materialize (no Cursor, no Janus)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from ar_eval.corpus import Episode, load_corpus, resolve_root
from ar_eval.materialize import materialize_episode


def test_load_corpus_example_only(tmp_path: Path) -> None:
    eval_dir = tmp_path / "eval"
    eval_dir.mkdir()
    (eval_dir / "corpus.example.yaml").write_text(
        yaml.dump(
            {
                "episodes": [
                    {"id": "a", "roots": ["fixtures/x"]},
                    {"id": "b", "roots": ["fixtures/y"]},
                ]
            }
        ),
        encoding="utf-8",
    )
    corpus = load_corpus(eval_dir=eval_dir, use_local=True)
    assert [e.id for e in corpus.episodes] == ["a", "b"]
    assert corpus.source_paths == [eval_dir / "corpus.example.yaml"]


def test_local_corpus_replaces_episodes(tmp_path: Path) -> None:
    eval_dir = tmp_path / "eval"
    eval_dir.mkdir()
    (eval_dir / "corpus.example.yaml").write_text(
        yaml.dump({"episodes": [{"id": "a", "roots": ["fixtures/x"]}]}),
        encoding="utf-8",
    )
    (eval_dir / "corpus.local.yaml").write_text(
        yaml.dump(
            {
                "episodes": [
                    {"id": "local-one", "roots": ["/tmp/does-not-need-exist-yet"]},
                ]
            }
        ),
        encoding="utf-8",
    )
    corpus = load_corpus(eval_dir=eval_dir, use_local=True)
    assert len(corpus.episodes) == 1
    assert corpus.episodes[0].id == "local-one"
    assert len(corpus.source_paths) == 2

    example_only = load_corpus(eval_dir=eval_dir, use_local=False)
    assert [e.id for e in example_only.episodes] == ["a"]


def test_resolve_root_relative_and_absolute(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "fixtures" / "demo").mkdir(parents=True)
    rel = resolve_root("fixtures/demo", repo_root=repo)
    assert rel == (repo / "fixtures" / "demo").resolve()
    abs_path = resolve_root(str(repo / "fixtures" / "demo"), repo_root=repo)
    assert abs_path == (repo / "fixtures" / "demo").resolve()


def test_materialize_single_and_combo(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    a = repo / "fixtures" / "alpha"
    b = repo / "fixtures" / "beta"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    (a / "a.py").write_text("a=1\n", encoding="utf-8")
    (b / "b.py").write_text("b=1\n", encoding="utf-8")
    subjects = tmp_path / "subjects"

    single = materialize_episode(
        Episode(id="one", roots=["fixtures/alpha"]),
        repo_root=repo,
        subjects_dir=subjects,
    )
    assert (single / "alpha" / "a.py").is_file()

    combo = materialize_episode(
        Episode(id="two", roots=["fixtures/alpha", "fixtures/beta"]),
        repo_root=repo,
        subjects_dir=subjects,
    )
    assert (combo / "alpha" / "a.py").is_file()
    assert (combo / "beta" / "b.py").is_file()
    assert (combo / ".git").exists()


def test_materialize_missing_root(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        materialize_episode(
            Episode(id="missing", roots=["nope"]),
            repo_root=tmp_path,
            subjects_dir=tmp_path / "subjects",
        )


@pytest.mark.skipif(
    not Path("fixtures/demo-workspace/alpha").is_dir(),
    reason="repo fixtures not present",
)
def test_load_committed_example_corpus() -> None:
    repo = Path(__file__).resolve().parents[1]
    eval_dir = repo / "eval"
    corpus = load_corpus(eval_dir=eval_dir, use_local=False)
    assert any(e.id == "demo-alpha" for e in corpus.episodes)
    assert any(e.id == "slop-twins" for e in corpus.episodes)
    for ep in corpus.episodes:
        for root in ep.roots:
            resolved = resolve_root(root, repo_root=repo)
            assert resolved.exists(), resolved
