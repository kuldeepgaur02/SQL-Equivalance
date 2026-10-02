"""Step 6 as a pipeline step."""
from __future__ import annotations

from typing import TYPE_CHECKING

from ..journal.entry import to_jsonable
from ..llm.oracle import Oracle
from .loader import Loader

if TYPE_CHECKING:
    from ..runner.batch import CaseContext


def insert_base(ctx: "CaseContext") -> None:
    oracle = ctx.state.get("oracle") or Oracle(ctx.settings, ctx.journal, ctx.memory)
    ctx.state["oracle"] = oracle
    with ctx.journal.timed("insert", "load the base rows (INSERT + repair loop)") as d:
        result = Loader(ctx.workspace, ctx.state["schema"], ctx.state["plan"], ctx.state["base"],
                        ctx.state.get("rules"), oracle, ctx.journal, ctx.memory).load()
        ctx.state["load"] = result
        d.update(
            inserted=[str(t) for t in result.rows],
            skipped={str(t): why for t, why in result.skipped.items()},
            repairs=[{"table": str(r.table), "attempt": r.attempt, "category": r.category, "method": r.method}
                     for r in result.repairs],
            updates=list(result.updates), failed_updates=list(result.failed_updates),
            rows_in_database={str(t): len(rows) for t, rows in result.snapshot.items()},
            base_rows={str(t): to_jsonable(row) for t, row in result.rows.items()},
            warnings=list(result.warnings),
        )
