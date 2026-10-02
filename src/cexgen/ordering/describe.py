"""Readable form of the insert plan."""
from __future__ import annotations

from .model import InsertPlan
from .planner import NOT_NEEDED


def describe_plan(plan: InsertPlan) -> str:
    lines = [f"insert plan (tables: {'those the queries need' if plan.scope == 'queries' else 'all'})"]
    for i, step in enumerate(plan.steps, 1):
        if step.kind == "cycle":
            lines.append(f"  {i}. together, one transaction: {', '.join(map(str, step.tables))}")
            lines.append(f"       deferred: {', '.join(fk.name for fk in step.deferred)}")
        else:
            lines.append(f"  {i}. {step.tables[0]}")
        for t in step.tables:
            for p in plan.fks_of(t):
                cols = f" ({', '.join(p.null_columns)} NULL)" if p.null_columns else ""
                lines.append(f"       {p.fk.name}: {p.strategy}{cols} - {p.reason}")
    if plan.updates:
        lines.append("  then UPDATE: " + ", ".join(f"{p.fk.table}.{p.fk.name}" for p in plan.updates))
    if plan.make_deferrable:
        lines.append("  made DEFERRABLE in the sandbox (changes when the FK is checked, not what): " + ", ".join(f"{fk.table}.{fk.name}" for fk in plan.make_deferrable))
    not_needed = [t for t, why in plan.skipped.items() if why == NOT_NEEDED]
    for t, why in plan.skipped.items():
        if why != NOT_NEEDED:
            lines.append(f"  skipped {t}: {why}")
    if not_needed:
        lines.append(f"  not needed by the queries: {', '.join(map(str, not_needed))}")
    for w in plan.warnings:
        lines.append(f"  warning: {w}")
    return "\n".join(lines)
