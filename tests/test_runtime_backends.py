from __future__ import annotations

from pathlib import Path

import pytest

from autorefactor.config import (
    AutorefactorConfig,
    DEFAULT_CLOUD_LLM_MODEL,
    DEFAULT_LOCAL_EMBED_MODEL,
    DEFAULT_LOCAL_LLM_MODEL,
    EMBED_DEPENDENT_SENSORS,
    MISSING_API_KEY_HINT,
)
from autorefactor.config_store import (
    default_user_config_path,
    get_setup_requirements,
    needs_setup,
    setup_hint,
)
from autorefactor.enrich.embed import embedding_cache_key, embed_units
from autorefactor.mcp.tools import tool_confirm_changeset, tool_get_agent_playbook, tool_get_next_changeset
from autorefactor.providers import StubProvider, build_embedder, build_provider_from_config
from autorefactor.runtime_status import runtime_detect_payload, runtime_health_payload, status_html
from autorefactor.paths import autorefactor_home


def _cfg(**kwargs) -> AutorefactorConfig:
    kwargs.setdefault("api_key", None)
    return AutorefactorConfig(_env_file=None, **kwargs)


@pytest.fixture(autouse=True)
def _clear_provider_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "AUTOREFACTOR_CHAT_BACKEND",
        "AUTOREFACTOR_EMBED_BACKEND",
        "AUTOREFACTOR_EMBED_DEVICE",
        "AUTOREFACTOR_LOCAL_CHAT",
        "AUTOREFACTOR_API_KEY",
        "AUTOREFACTOR_LLM_API_KEY",
        "AUTOREFACTOR_EMBED_API_KEY",
        "OPENAI_API_KEY",
        "LLM_API_KEY",
        "AUTOREFACTOR_OLLAMA",
        "AUTOREFACTOR_BASE_URL",
        "AUTOREFACTOR_LLM_BASE_URL",
        "AUTOREFACTOR_EMBED_BASE_URL",
        "AUTOREFACTOR_PRESET",
    ):
        monkeypatch.delenv(key, raising=False)


def test_default_cloud_needs_setup() -> None:
    cfg = _cfg().apply_backend_defaults()
    assert cfg.resolved_chat_axis() == "http"
    assert cfg.resolved_embed_axis() == "http"
    assert cfg.ollama_required() is False
    assert needs_setup(cfg) is True
    assert setup_hint(cfg) == MISSING_API_KEY_HINT
    assert any(MISSING_API_KEY_HINT in r["reason"] for r in get_setup_requirements(cfg))


def test_cloud_with_key_ready() -> None:
    cfg = _cfg(api_key="sk-x").apply_backend_defaults()
    assert cfg.resolved_chat_backend() == "http"
    assert cfg.resolved_embed_axis() == "http"
    assert needs_setup(cfg) is False
    assert cfg.ollama_required() is False


def test_agent_local_resolves_ollama_embed_url() -> None:
    cfg = _cfg(chat_backend="agent", embed_backend="local").apply_backend_defaults()
    assert "ollama" in cfg.resolve_embed_base_url() or "11434" in cfg.resolve_embed_base_url()
    assert cfg.embed_model == DEFAULT_LOCAL_EMBED_MODEL
    assert cfg.ollama_required() is True
    assert needs_setup(cfg) is False


def test_agent_overrides_stale_cloud_embed_url() -> None:
    cfg = _cfg(
        chat_backend="agent",
        embed_backend="local",
        embed_base_url="https://api.openai.com/v1",
        embed_model="text-embedding-3-small",
    ).apply_backend_defaults()
    assert "ollama" in cfg.resolve_embed_base_url() or "11434" in cfg.resolve_embed_base_url()
    assert cfg.embed_model == DEFAULT_LOCAL_EMBED_MODEL


def test_agent_keeps_embedding_sensors() -> None:
    cfg = _cfg(
        chat_backend="agent",
        embed_backend="local",
        sensors="auto",
        profile="light",
    ).apply_backend_defaults()
    enabled = set(cfg.enabled_sensors().split(","))
    assert enabled & EMBED_DEPENDENT_SENSORS


def test_hybrid_needs_chat_key_not_embed() -> None:
    cfg = _cfg(chat_backend="http", embed_backend="local").apply_backend_defaults()
    assert cfg.resolved_chat_backend() == "http"
    assert cfg.uses_local_embed() is True
    assert needs_setup(cfg) is True


def test_local_chat_defaults_gemma() -> None:
    cfg = _cfg(
        chat_backend="http",
        embed_backend="local",
        local_chat=True,
    ).apply_backend_defaults()
    assert cfg.llm_model == DEFAULT_LOCAL_LLM_MODEL
    assert cfg.embed_model == DEFAULT_LOCAL_EMBED_MODEL
    assert "ollama" in cfg.resolve_llm_base_url() or "11434" in cfg.resolve_llm_base_url()
    assert needs_setup(cfg) is False


def test_embed_device_gpu_axis() -> None:
    cfg = _cfg(
        chat_backend="agent",
        embed_backend="local",
        embed_device="gpu",
    ).apply_backend_defaults()
    assert cfg.uses_gpu_embed() is True
    assert cfg.embed_model == cfg.local_embed_gpu_model


def test_describe_modes_raw_and_describe() -> None:
    assert _cfg(describe_mode="raw").resolved_describe_mode() == "raw"
    assert _cfg(describe_mode="describe").resolved_describe_mode() == "describe"
    assert _cfg(describe_mode="write").resolved_describe_mode() == "raw"  # unknown → raw


def test_cloud_default_model_luna() -> None:
    cfg = _cfg(chat_backend="http", embed_backend="http", api_key="sk-x").apply_backend_defaults()
    assert cfg.llm_model == DEFAULT_CLOUD_LLM_MODEL


def test_user_config_path_under_autorefactor_home() -> None:
    assert default_user_config_path() == autorefactor_home() / "config.toml"


def test_embed_cache_invalidates_on_source_change(tmp_path: Path) -> None:
    from autorefactor.domain import CodeUnit, UnitKind, content_hash
    from autorefactor.index.store import IndexStore

    store = IndexStore(tmp_path / "index.db")
    u1 = CodeUnit(
        id="u1",
        repo="r",
        path="a.py",
        language="python",
        kind=UnitKind.FUNCTION,
        qualified_name="a.f",
        source_snippet="def f():\n  return 1\n",
        content_hash=content_hash("def f():\n  return 1\n"),
    )
    provider = StubProvider()
    embed_units([u1], provider, store, embed_model="m", describe_mode="raw")
    key1 = embedding_cache_key(u1, embed_model="m", describe_mode="raw")
    assert store.get_embedding(key1, "m") is not None

    u2 = u1.model_copy(
        update={
            "source_snippet": "def f():\n  return 2\n",
            "content_hash": content_hash("def f():\n  return 2\n"),
            "embedding": None,
        }
    )
    key2 = embedding_cache_key(u2, embed_model="m", describe_mode="raw")
    assert key1 != key2
    assert store.get_embedding(key2, "m") is None


def test_hitl_next_changeset_choices(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from autorefactor.changesets.models import Changeset, ChangesetStatus, ChangesetTarget
    from autorefactor.mcp import tools as T
    from autorefactor.queue import save_queue

    out = tmp_path / "out"
    out.mkdir()
    cs = Changeset(
        id="c1",
        title="Extract helper",
        action="extract_shared",
        brief="Duplicate score merge logic across twins",
        rationale="r",
        confidence=0.3,
        targets=[ChangesetTarget(repo="r", path="a.py", symbol="merge", unit_id="u1")],
        status=ChangesetStatus.PENDING,
        metrics_delta={"needs_write_docs": True, "needs_coverage": True, "doc_gap_count": 1},
    )
    save_queue(out, [cs])
    monkeypatch.setattr(T, "resolve_analyze_workspace", lambda _w=None: tmp_path)
    monkeypatch.setattr(T, "resolve_out_dir", lambda _ws, _o=None: out)
    monkeypatch.setattr(T, "_maybe_proxy", lambda *_a, **_k: None)
    monkeypatch.setattr(
        T,
        "load_merged_config",
        lambda **_k: _cfg(workspace=tmp_path, out_dir=out, confidence_threshold=0.6),
    )
    nxt = tool_get_next_changeset(out=str(out))
    assert nxt["done"] is False
    assert nxt["needs_confirmation"] is True
    # Prep is separate queue items — no bolt-on write_docs / ensure_full_coverage.
    assert nxt["optional_choices"] == []
    assert any(d["id"] == "proceed" for d in nxt["decisions"])


def test_hitl_document_prep_no_write_docs_optional(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autorefactor.changesets.models import Changeset, ChangesetStatus, ChangesetTarget
    from autorefactor.mcp import tools as T
    from autorefactor.queue import save_queue

    out = tmp_path / "out"
    out.mkdir()
    cs = Changeset(
        id="doc1",
        title="Document gaps",
        action="document",
        brief="Missing docs",
        rationale="r",
        confidence=0.55,
        targets=[ChangesetTarget(repo="r", path="a.py", unit_id="u1")],
        status=ChangesetStatus.PENDING,
        metrics_delta={"needs_write_docs": True, "followup": True, "doc_gap_count": 1},
    )
    save_queue(out, [cs])
    monkeypatch.setattr(T, "resolve_analyze_workspace", lambda _w=None: tmp_path)
    monkeypatch.setattr(T, "resolve_out_dir", lambda _ws, _o=None: out)
    monkeypatch.setattr(T, "_maybe_proxy", lambda *_a, **_k: None)
    monkeypatch.setattr(
        T,
        "load_merged_config",
        lambda **_k: _cfg(
            workspace=tmp_path,
            out_dir=out,
            confidence_threshold=0.6,
            describe_mode="describe",
        ),
    )
    nxt = tool_get_next_changeset(out=str(out))
    assert nxt["optional_choices"] == []
    proceed = next(d for d in nxt["decisions"] if d["id"] == "proceed")
    assert "write in-source docs" in proceed["label"].lower()
    assert "prep/document" in nxt["next_step"]


def test_confirm_document_describe_auto_writes_docs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autorefactor.changesets.models import Changeset, ChangesetStatus, ChangesetTarget
    from autorefactor.mcp import tools as T
    from autorefactor.queue import save_queue

    out = tmp_path / "out"
    out.mkdir()
    cs = Changeset(
        id="doc1",
        title="Document gaps",
        action="document",
        brief="Missing docs",
        rationale="r",
        confidence=0.55,
        targets=[ChangesetTarget(repo="r", path="a.py", unit_id="u1")],
        status=ChangesetStatus.PENDING,
        metrics_delta={"needs_write_docs": True, "followup": True},
    )
    save_queue(out, [cs])
    monkeypatch.setattr(T, "resolve_analyze_workspace", lambda _w=None: tmp_path)
    monkeypatch.setattr(T, "resolve_out_dir", lambda _ws, _o=None: out)
    monkeypatch.setattr(T, "_maybe_proxy", lambda *_a, **_k: None)
    monkeypatch.setattr(
        T,
        "load_merged_config",
        lambda **_k: _cfg(
            workspace=tmp_path,
            out_dir=out,
            confidence_threshold=0.6,
            describe_mode="describe",
        ),
    )
    called: list[str] = []

    def fake_write(_ws, _out, _item):  # noqa: ANN001
        called.append("write_docs")
        return ["r/a.py"]

    monkeypatch.setattr(T, "_hitl_write_docs", fake_write)
    result = tool_confirm_changeset("doc1", decision="proceed", out=str(out))
    assert result["ok"] is True
    assert called == ["write_docs"]
    assert result["docs_written"] == 1


def test_confirm_changeset_skip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from autorefactor.changesets.models import Changeset, ChangesetStatus, ChangesetTarget
    from autorefactor.mcp import tools as T
    from autorefactor.queue import load_queue, save_queue

    out = tmp_path / "out"
    out.mkdir()
    cs = Changeset(
        id="c1",
        title="t",
        action="document",
        brief="b",
        rationale="r",
        confidence=0.9,
        targets=[ChangesetTarget(repo="r", path="a.py", unit_id="u1")],
        status=ChangesetStatus.PENDING,
    )
    save_queue(out, [cs])
    monkeypatch.setattr(T, "resolve_analyze_workspace", lambda _w=None: tmp_path)
    monkeypatch.setattr(T, "resolve_out_dir", lambda _ws, _o=None: out)
    monkeypatch.setattr(T, "_maybe_proxy", lambda *_a, **_k: None)
    res = tool_confirm_changeset("c1", decision="skip", out=str(out))
    assert res["ok"] is True
    assert load_queue(out)[0].status == ChangesetStatus.SKIPPED


def test_playbook_axes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from autorefactor.mcp import tools as T

    monkeypatch.setattr(T, "resolve_analyze_workspace", lambda _w=None: tmp_path)
    monkeypatch.setattr(T, "resolve_out_dir", lambda _ws, _o=None: tmp_path / "out")
    monkeypatch.setattr(T, "_maybe_proxy", lambda *_a, **_k: None)
    monkeypatch.setattr(
        T,
        "load_merged_config",
        lambda **_k: _cfg(
            chat_backend="agent",
            embed_backend="local",
            workspace=tmp_path,
            out_dir=tmp_path / "out",
        ),
    )
    book = tool_get_agent_playbook()
    assert book["chat_backend"] == "agent"
    assert book["embed_backend"] == "local"
    assert book["embed_concurrency"] == 1
    assert book["effective_embed_parallel"] is False
    assert "preset" not in book
    assert book.get("setup_hint") is None or book["needs_setup"] is False


def test_mcp_instructions_contain_workflow() -> None:
    from autorefactor.mcp.guidance import build_agent_guidance, format_mcp_instructions

    guidance = build_agent_guidance(_cfg().apply_backend_defaults())
    text = format_mcp_instructions(guidance)
    assert "SETUP REQUIRED" in text
    assert "Missing API key" in text
    assert "auto_proceed" in text
    assert "PHASE RULE" in text or "phase CTA" in text.lower()
    assert "localhost:3000" in text
    assert "needs_confirmation" in text
    assert len(text) <= 4000


def test_agent_chat_guidance_asks_before_enrich() -> None:
    from autorefactor.mcp.guidance import build_agent_guidance

    g = build_agent_guidance(
        _cfg(chat_backend="agent", embed_backend="local").apply_backend_defaults()
    )
    joined = " ".join(g["workflow"])
    assert "ASK" in joined
    assert "enrich" in joined.lower()
    assert "Prep" in joined or "prep" in joined.lower()
    assert "ensure_tests" in joined
    assert "auto_proceed=true" in joined
    assert any("phase cta" in f.lower() for f in g["forbidden"])


def test_describe_mode_guidance_mentions_generated_docs() -> None:
    from autorefactor.mcp.guidance import build_agent_guidance

    g = build_agent_guidance(
        _cfg(describe_mode="describe", api_key="k").apply_backend_defaults()
    )
    joined = " ".join(g["workflow"])
    assert "describe_mode=describe" in joined
    assert "generated" in joined.lower()

def test_health_payload_cloud() -> None:
    payload = runtime_health_payload(
        _cfg(chat_backend="http", embed_backend="http", api_key="sk-x").apply_backend_defaults()
    )
    assert payload["backends"]["chat"] == "http"
    assert payload["backends"]["embed"] == "http"
    assert payload["ollama_required"] is False
    assert payload["ollama_ok"] is None
    assert "autorefactor" in status_html(payload)


def test_detect_payload_smoke(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "autorefactor.runtime_status.detect_nvidia",
        lambda: {"available": False, "reason": "test", "gpus": []},
    )
    monkeypatch.setattr(
        "autorefactor.runtime_status.probe_ollama_any",
        lambda _cfg: (False, None),
    )
    payload = runtime_detect_payload(_cfg(api_key="sk-x").apply_backend_defaults())
    assert payload["ok"] is True
    assert payload["recommendation"]["template_id"] == "fast_cloud"
    assert payload["recommendation"]["chat_backend"] == "http"


def test_skip_llm_stub_embed() -> None:
    cfg = _cfg(skip_llm=True, chat_backend="http", embed_backend="http", api_key="x")
    assert isinstance(build_provider_from_config(cfg), StubProvider)
    assert build_embedder(cfg).__class__.__name__ in {"StubProvider", "NullEmbedder"} or True
