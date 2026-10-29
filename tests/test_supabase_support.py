from __future__ import annotations

import json
from pathlib import Path

from autorefactor.changesets.models import Changeset
from autorefactor.changesets.store import security_sensitive_hitl, slop_kind_hitl
from autorefactor.config import AutorefactorConfig
from autorefactor.domain import CodeUnit, UnitKind
from autorefactor.enrich.coverage_parsers import (
    discover_coverage_reports,
    parse_lcov_info,
)
from autorefactor.extract.registry import extract_repo
from autorefactor.extract.sql import SqlExtractor
from autorefactor.sensors.base import AnalysisContext
from autorefactor.sensors.coverage import coverage_tool_configured
from autorefactor.sensors.supabase import SupabaseSensor
from autorefactor.testing.toolchains import coverage_augmented_command

MIGRATION = """\
create table public.profiles (
  id uuid primary key,
  email text
);

create table public.open_secrets (
  data text
);

create table public.lonely (id int primary key);
alter table public.lonely enable row level security;

create table public._audit_log (id int);
create table storage.objects (id int);

alter table public.profiles enable row level security;

create policy "profiles_read" on public.profiles
  for select to authenticated using (true);

create policy "broad" on public.open_secrets
  for all using (true);

create or replace function public.bump_counter() returns trigger
language plpgsql as $$
begin
  return new;
end;
$$;

create trigger bump_counter after insert on public.profiles
  execute function public.bump_counter();

create index profiles_email_idx on public.profiles (email);
"""


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _ctx(workspace: Path, repo: Path, units: list[CodeUnit]) -> AnalysisContext:
    return AnalysisContext(
        workspace=workspace,
        repos=(repo,),
        units=tuple(units),
        config=AutorefactorConfig(workspace=workspace, out_dir=workspace / "out"),
        focus="balanced",
    )


def test_sql_extractor_units(tmp_path: Path) -> None:
    repo = tmp_path / "app"
    _write(repo, "supabase/migrations/0001_init.sql", MIGRATION)

    units = SqlExtractor().extract("app", repo)
    kinds = {u.kind for u in units}
    assert UnitKind.SQL_TABLE in kinds
    assert UnitKind.SQL_POLICY in kinds
    assert UnitKind.SQL_FUNCTION in kinds
    assert UnitKind.SQL_TRIGGER in kinds
    assert UnitKind.SQL_INDEX in kinds

    tables = {u.qualified_name for u in units if u.kind == UnitKind.SQL_TABLE}
    assert "0001_init.public.profiles" in tables
    table_unit = next(u for u in units if u.qualified_name == "0001_init.public.profiles")
    assert table_unit.language == "sql"
    assert "email text" in table_unit.source_snippet

    fn = next(u for u in units if u.kind == UnitKind.SQL_FUNCTION)
    assert "return new" in fn.source_snippet  # dollar-quoted body captured

    file_unit = next(u for u in units if u.kind == UnitKind.FILE)
    assert file_unit.language == "sql"


def test_supabase_sensor_rls_findings(tmp_path: Path) -> None:
    repo = tmp_path / "app"
    _write(repo, "supabase/migrations/0001_init.sql", MIGRATION)
    units = extract_repo(repo)
    ctx = _ctx(tmp_path, repo, units)

    assert SupabaseSensor().available(ctx)
    findings = SupabaseSensor().collect(ctx)
    by_kind: dict[str, list] = {}
    for f in findings:
        by_kind.setdefault(str(f.metadata.get("kind")), []).append(f)

    # open_secrets: policy exists but RLS never enabled -> still open
    missing_tables = {f.metadata["table"] for f in by_kind.get("missing_rls", [])}
    assert "public.open_secrets" in missing_tables
    # lonely: RLS enabled, no policies -> deny-all trap
    no_policy_tables = {f.metadata["table"] for f in by_kind.get("rls_no_policies", [])}
    assert "public.lonely" in no_policy_tables
    # profiles: enabled + policy -> no RLS finding
    assert "public.profiles" not in missing_tables
    assert "public.profiles" not in no_policy_tables
    # internal/underscore tables skipped
    assert "public._audit_log" not in missing_tables
    assert "storage.objects" not in missing_tables
    # permissive USING (true) policies flagged
    permissive = by_kind.get("permissive_rls_policy", [])
    assert len(permissive) == 2  # profiles_read + broad

    # HITL mapping: security kinds never auto-proceed
    secret_cs = Changeset(
        id="cs1",
        title="x",
        action="document",
        brief="b",
        rationale="r",
        confidence=0.95,
        metrics_delta={"kind": "missing_rls"},
    )
    assert security_sensitive_hitl(secret_cs) is True


def test_supabase_sensor_client_findings(tmp_path: Path) -> None:
    repo = tmp_path / "app"
    _write(
        repo,
        "src/lib/admin.ts",
        'import { createClient } from "@supabase/supabase-js";\n'
        'const key = process.env.SUPABASE_SERVICE_ROLE_KEY ?? "eyJhbGciOiJIUzI1NiJ9.abc.def";\n'
        "export const admin = createClient(url, key);\n",
    )
    _write(
        repo,
        "src/components/AdminPanel.tsx",
        '"use client";\n'
        'import { createClient } from "@supabase/supabase-js";\n'
        "const client = createClient(url, process.env.SUPABASE_SERVICE_ROLE_KEY!);\n",
    )
    _write(
        repo,
        "src/app/rows.ts",
        'import { createClient } from "@supabase/supabase-js";\n'
        "export async function rows() {\n"
        "  const { data } = await supabase.from('t').select('*');\n"
        "  const a = data as any;\n"
        "  const b: any = a;\n"
        "  const c = data as any;\n"
        "  return [a, b, c];\n"
        "}\n",
    )
    units = extract_repo(repo)
    ctx = _ctx(tmp_path, repo, units)
    findings = SupabaseSensor().collect(ctx)
    kinds = {str(f.metadata.get("kind")) for f in findings}

    assert "committed_secret" in kinds
    assert "service_role_in_client" in kinds
    assert "untyped_supabase_response" in kinds

    untyped_cs = Changeset(
        id="cs2",
        title="y",
        action="document",
        brief="b",
        rationale="r",
        confidence=0.95,
        metrics_delta={"kind": "untyped_supabase_response"},
    )
    assert slop_kind_hitl(untyped_cs) is True


def test_coverage_augments_pnpm_with_c8_targeting_pnpm() -> None:
    out = coverage_augmented_command("pnpm test", [])
    assert "c8" in out
    assert "pnpm exec c8" in out
    assert out.rstrip().endswith("pnpm test")


def test_vitest_counts_as_configured_coverage(tmp_path: Path) -> None:
    repo = tmp_path / "www"
    _write(repo, "package.json", json.dumps({"devDependencies": {"vitest": "^3.0.0"}}))
    assert coverage_tool_configured(repo)

    plain = tmp_path / "plain"
    _write(plain, "package.json", json.dumps({"name": "plain"}))
    assert not coverage_tool_configured(plain)


def test_lcov_parser_and_discovery(tmp_path: Path) -> None:
    repo = tmp_path / "www"
    _write(
        repo,
        "coverage/lcov.info",
        "TN:\n"
        f"SF:{repo}/src/lib/util.ts\n"
        "FN:1,10\n"
        "LF:10\n"
        "LH:7\n"
        "end_of_record\n",
    )
    snap = parse_lcov_info(repo / "coverage" / "lcov.info")
    assert snap.source == "lcov"
    assert len(snap.files) == 1
    stat = snap.files[0]
    assert stat.path == "src/lib/util.ts"  # absolute path shortened to source root
    assert stat.total_lines == 10
    assert stat.covered_lines == 7
    assert snap.overall_percent == 70.0

    reports = discover_coverage_reports(tmp_path, [repo])
    assert any(p.name == "lcov.info" for p in reports)
