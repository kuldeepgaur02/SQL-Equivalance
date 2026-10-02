"""Step 5 as a pipeline step."""
from __future__ import annotations

from typing import TYPE_CHECKING

from ..journal.entry import to_jsonable
from ..llm.oracle import Oracle
from .builder import build_base

if TYPE_CHECKING:
    from ..runner.batch import CaseContext


def build_base_data(ctx: "CaseContext") -> None:
    oracle = ctx.state.get("oracle") or Oracle(ctx.settings, ctx.journal, ctx.memory)
    ctx.state["oracle"] = oracle
    with ctx.journal.timed("base_data", "build one row per table") as d:
        base = build_base(ctx.state["schema"], ctx.state["plan"], ctx.state.get("rules"), ctx.state.get("queries"),
                          oracle)
        ctx.state["base"] = base
        d.update(
            mode=oracle.mode,
            rows={str(t): {c: {"value": to_jsonable(cell.value), "source": cell.source}
                           for c, cell in row.items()} for t, row in base.rows.items()},
            updates=[{"table": str(u.table), "fk": u.fk.name, "set": to_jsonable(dict(u.values))} for u in base.updates],
            left_to_repair=list(base.unsatisfied), warnings=list(base.warnings), llm_calls=oracle.calls,
        )
