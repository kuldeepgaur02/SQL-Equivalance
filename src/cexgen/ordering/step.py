"""Step 4 as a pipeline step: the insert plan for this case's queries."""
from __future__ import annotations

from typing import TYPE_CHECKING

from .planner import NOT_NEEDED, plan_inserts

if TYPE_CHECKING:
    from ..runner.batch import CaseContext


def plan_order(ctx: "CaseContext") -> None:
    with ctx.journal.timed("order", "plan table order and how each foreign key is satisfied") as d:
        plan = plan_inserts(ctx.state["schema"], ctx.state.get("queries"), ctx.state.get("rules"))
        ctx.state["plan"] = plan
        d.update(
            scope=plan.scope,
            steps=[[str(t) for t in s.tables] for s in plan.steps],
            fks={f"{p.fk.table}.{p.fk.name}": p.strategy for p in plan.fks},
            skipped={str(t): why for t, why in plan.skipped.items() if why != NOT_NEEDED},
            not_needed=[str(t) for t, why in plan.skipped.items() if why == NOT_NEEDED],
            make_deferrable=[f"{fk.table}.{fk.name}" for fk in plan.make_deferrable],
            warnings=list(plan.warnings),
        )
