"""Compat shim — prefer autorefactor.plan.plan_findings."""

from autorefactor.plan.planner import plan_findings


def reconcile_flags(flags, units, provider, *, concurrency: int = 8):
    confirmed, changesets, _decisions, _agenda = plan_findings(
        flags, units, provider, concurrency=concurrency
    )
    return confirmed, changesets
