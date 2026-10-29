"""Supabase access-policy and client-hygiene sensor (RLS, service-role keys, typed results)."""

from __future__ import annotations

import re
from collections import defaultdict

from autorefactor.domain import (
    AnalysisGoal,
    CodeUnit,
    Finding,
    Severity,
    SuggestedAction,
    UnitKind,
    content_hash,
    try_make_finding,
)
from autorefactor.paths import resolve_repo_file
from autorefactor.sensors.base import AnalysisContext
from autorefactor.sensors.similarity import is_test_path

_INTERNAL_TABLE = re.compile(
    r"^(storage|extensions|graphql|realtime|supabase_migrations|vault|cron|auth)\."
    r"|^(_|cf_)|schema_migrations$",
    re.IGNORECASE,
)


def _rls_enable_for(table: str) -> re.Pattern[str]:
    return re.compile(
        rf"alter\s+table\s+(?:only\s+)?(?:\w+\.)?{re.escape(table)}\b"
        r"[^;]*enable\s+row\s+level\s+security",
        re.IGNORECASE,
    )


def _policy_on_for(table: str) -> re.Pattern[str]:
    return re.compile(rf"\bon\s+(?:\w+\.)?{re.escape(table)}\b", re.IGNORECASE)


_ALWAYS_TRUE = re.compile(r"\b(?:using|with\s+check)\s*\(?\s*true\s*\)?", re.IGNORECASE)
_TABLE_IN_POLICY = re.compile(r"\bon\s+([\w.\"]+)", re.IGNORECASE)
_SERVICE_ROLE = re.compile(r"service_role", re.IGNORECASE)
_JWT_LITERAL = re.compile(r"eyJ[A-Za-z0-9_-]{10,}")
_USE_CLIENT = re.compile(r"""["']use\s+client["']""")
_SUPABASE_CLIENT = re.compile(r"@supabase/|createClient\s*\(")
_ANY_CAST = re.compile(r"\bas\s+any\b|:\s*any\b")
_CREATE_TABLE_HEAD = re.compile(
    r"create\s+table\s+(?:if\s+not\s+exists\s+)?([\w.\"]+)\s*\(",
    re.IGNORECASE,
)

_TS_LANGS = frozenset({"typescript", "tsx", "javascript"})
_MAX_PER_KIND = {
    "missing_rls": 10,
    "rls_no_policies": 6,
    "permissive_rls_policy": 8,
    "committed_secret": 6,
    "service_role_in_client": 4,
    "untyped_supabase_response": 6,
}


def _read_unit_file(ctx: AnalysisContext, unit: CodeUnit) -> str:
    try:
        path = resolve_repo_file(ctx.workspace, unit.repo, unit.path)
        return path.read_text(encoding="utf-8", errors="replace")[:300_000]
    except OSError:
        return unit.source_snippet or ""


def _table_name(unit: CodeUnit) -> str:
    match = _CREATE_TABLE_HEAD.search(unit.source_snippet or "")
    if match:
        return match.group(1).strip('"').lower()
    return unit.qualified_name.rsplit(".", 1)[-1].strip('"').lower()


def _base_name(table: str) -> str:
    return table.split(".")[-1]


class SupabaseSensor:
    """Flag missing/permissive RLS, service-role exposure, and untyped Supabase results."""

    id = "supabase"
    goal = AnalysisGoal.STRUCTURE

    def available(self, ctx: AnalysisContext) -> bool:
        for unit in ctx.units:
            if unit.language == "sql":
                return True
            if unit.kind == UnitKind.FILE and "supabase" in (unit.source_snippet or "").lower():
                return True
        return False

    def collect(self, ctx: AnalysisContext) -> list[Finding]:
        known = ctx.known_ids()
        findings: list[Finding] = []
        findings.extend(self._rls_findings(ctx, known))
        findings.extend(self._client_findings(ctx, known))
        return findings

    def _rls_findings(self, ctx: AnalysisContext, known: set[str]) -> list[Finding]:
        sql_units = [u for u in ctx.units_list() if u.language == "sql"]
        tables = [u for u in sql_units if u.kind == UnitKind.SQL_TABLE]
        policies = [u for u in sql_units if u.kind == UnitKind.SQL_POLICY]
        if not tables:
            return []

        # Full file text per repo (RLS toggles live in ALTER TABLE, not in table units).
        sql_text: dict[str, list[str]] = defaultdict(list)
        for unit in sql_units:
            if unit.kind == UnitKind.FILE:
                sql_text[unit.repo].append(_read_unit_file(ctx, unit))

        findings: list[Finding] = []
        per_repo_counts: dict[tuple[str, str], int] = defaultdict(int)
        policy_text = [(p, (p.source_snippet or "")) for p in policies]

        for table in tables:
            name = _table_name(table)
            base = _base_name(name)
            if _INTERNAL_TABLE.search(name) or _INTERNAL_TABLE.search(base):
                continue
            on_table = [p for p, text in policy_text if _policy_on_for(base).search(text)]
            rls_enabled = any(
                _rls_enable_for(base).search(blob) for blob in sql_text.get(table.repo, [])
            )
            kind = None
            if not rls_enabled:
                kind = "missing_rls"
                title = f"Table `{name}` has no RLS in {table.repo}"
                if on_table:
                    evidence = (
                        f"`{name}` has {len(on_table)} policy statement(s) but row level "
                        "security is never enabled — policies are inert and the table stays "
                        "open to the anon/authenticated keys. Enable RLS (or drop the table)."
                    )
                else:
                    evidence = (
                        f"`{name}` is created without `ENABLE ROW LEVEL SECURITY` and no policy "
                        "targets it — with the anon/authenticated keys the table is open. "
                        "Enable RLS and add least-privilege policies (or drop the table)."
                    )
                severity, score = Severity.HIGH, 0.9
            elif not on_table:
                kind = "rls_no_policies"
                title = f"Table `{name}` has RLS enabled but zero policies in {table.repo}"
                evidence = (
                    f"`{name}` enables row level security but no policy targets it — access is "
                    "denied for every role (wire the intended policies or remove the table usage)."
                )
                severity, score = Severity.MEDIUM, 0.78
            if kind is None:
                continue
            if per_repo_counts[(table.repo, kind)] >= _MAX_PER_KIND[kind]:
                continue
            per_repo_counts[(table.repo, kind)] += 1
            f = try_make_finding(
                id=content_hash(f"supabase:{kind}:{table.repo}:{name}"),
                goal=self.goal,
                sensor=self.id,
                severity=severity,
                title=title,
                evidence=evidence,
                unit_ids=[table.id],
                action_hint=SuggestedAction.DOCUMENT,
                score=score,
                metadata={"kind": kind, "repo": table.repo, "table": name},
                known_ids=known,
            )
            if f:
                findings.append(f)

        for policy, text in policy_text:
            match = _TABLE_IN_POLICY.search(text)
            target = match.group(1).strip('"').lower() if match else ""
            if target and (
                _INTERNAL_TABLE.search(target)
                or _INTERNAL_TABLE.search(_base_name(target))
            ):
                continue
            if not _ALWAYS_TRUE.search(text):
                continue
            count_key = (policy.repo, "permissive_rls_policy")
            if per_repo_counts[count_key] >= _MAX_PER_KIND["permissive_rls_policy"]:
                continue
            per_repo_counts[count_key] += 1
            f = try_make_finding(
                id=content_hash(f"supabase:permissive_rls:{policy.id}"),
                goal=self.goal,
                sensor=self.id,
                severity=Severity.HIGH,
                title=f"Permissive RLS policy `{policy.qualified_name}` in {policy.repo}",
                evidence=(
                    f"Policy on `{target or 'table'}` uses an always-true predicate "
                    "(`USING/WITH CHECK true`) — it grants unrestricted access. Tighten the "
                    "predicate to the intended rows or remove the policy."
                ),
                unit_ids=[policy.id],
                action_hint=SuggestedAction.DOCUMENT,
                score=0.86,
                metadata={
                    "kind": "permissive_rls_policy",
                    "repo": policy.repo,
                    "table": target,
                },
                known_ids=known,
            )
            if f:
                findings.append(f)
        return findings

    def _client_findings(self, ctx: AnalysisContext, known: set[str]) -> list[Finding]:
        findings: list[Finding] = []
        counts: dict[str, int] = defaultdict(int)

        for unit in ctx.units_list():
            if unit.kind != UnitKind.FILE or unit.language not in _TS_LANGS:
                continue
            if is_test_path(unit.path):
                continue
            text = _read_unit_file(ctx, unit)
            if not text:
                continue

            has_service = bool(_SERVICE_ROLE.search(text))
            has_jwt = bool(_JWT_LITERAL.search(text))
            if (
                has_service
                and has_jwt
                and counts["committed_secret"] < _MAX_PER_KIND["committed_secret"]
            ):
                counts["committed_secret"] += 1
                f = try_make_finding(
                    id=content_hash(f"supabase:service_role_secret:{unit.id}"),
                    goal=self.goal,
                    sensor=self.id,
                    severity=Severity.HIGH,
                    title=f"Service-role key embedded in `{unit.path}` ({unit.repo})",
                    evidence=(
                        f"`{unit.path}` combines a `service_role` reference with a hardcoded JWT "
                        "literal — rotate the key and read it from env/vault on the server only."
                    ),
                    unit_ids=[unit.id],
                    action_hint=SuggestedAction.DOCUMENT,
                    score=0.92,
                    metadata={
                        "kind": "committed_secret",
                        "repo": unit.repo,
                        "path": unit.path,
                    },
                    known_ids=known,
                )
                if f:
                    findings.append(f)
                continue

            if (
                has_service
                and _USE_CLIENT.search(text)
                and counts["service_role_in_client"] < _MAX_PER_KIND["service_role_in_client"]
            ):
                counts["service_role_in_client"] += 1
                f = try_make_finding(
                    id=content_hash(f"supabase:service_role_client:{unit.id}"),
                    goal=self.goal,
                    sensor=self.id,
                    severity=Severity.HIGH,
                    title=f"Service-role referenced from client component `{unit.path}`",
                    evidence=(
                        f"`{unit.path}` is a browser component (`\"use client\"`) that references "
                        "the service-role key — anything shipped to the browser bypasses RLS. "
                        "Move the call behind a server route and drop the client reference."
                    ),
                    unit_ids=[unit.id],
                    action_hint=SuggestedAction.DOCUMENT,
                    score=0.85,
                    metadata={
                        "kind": "service_role_in_client",
                        "repo": unit.repo,
                        "path": unit.path,
                    },
                    known_ids=known,
                )
                if f:
                    findings.append(f)
                continue

            any_hits = len(_ANY_CAST.findall(text))
            if (
                _SUPABASE_CLIENT.search(text)
                and any_hits >= 3
                and counts["untyped_supabase_response"] < _MAX_PER_KIND["untyped_supabase_response"]
            ):
                counts["untyped_supabase_response"] += 1
                f = try_make_finding(
                    id=content_hash(f"supabase:untyped_response:{unit.id}"),
                    goal=self.goal,
                    sensor=self.id,
                    severity=Severity.MEDIUM,
                    title=f"Untyped Supabase results in `{unit.path}` ({unit.repo})",
                    evidence=(
                        f"`{unit.path}` casts Supabase results with `any` ({any_hits}×) — replace "
                        "with generated database types (supabase gen types) so refactors stay "
                        "type-safe."
                    ),
                    unit_ids=[unit.id],
                    action_hint=SuggestedAction.DOCUMENT,
                    score=0.7,
                    metadata={
                        "kind": "untyped_supabase_response",
                        "repo": unit.repo,
                        "path": unit.path,
                    },
                    known_ids=known,
                )
                if f:
                    findings.append(f)
        return findings
