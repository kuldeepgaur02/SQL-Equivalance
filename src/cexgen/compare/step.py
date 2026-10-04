"""Step 7 as a pipeline step: run Q1 and Q2 on the loaded data and compare."""
from __future__ import annotations

from typing import TYPE_CHECKING

from .compare import compare
from .execute import run_pair
from .model import DIFFER, Comparison
from .normalize import display
from .ordering import nondeterminism

if TYPE_CHECKING:
    from ..runner.batch import CaseContext


def run_comparison(ctx: "CaseContext") -> Comparison:
    """Run Q1 and Q2 on what is in the database now, and compare (also used by Step 8's rounds)."""
    q1, q2 = ctx.case.q1, ctx.case.q2
    r1, r2 = run_pair(ctx.conn, q1, q2, ctx.settings.max_result_rows)
    flags = [f"{label}: {why}" for label, sql in (("Q1", q1), ("Q2", q2)) for why in nondeterminism(sql)]
    queries = ctx.state.get("queries")
    if queries is not None:
        flags += [f"{q.label} calls volatile function(s) {', '.join(q.volatile_functions)}"
                  for q in (queries.q1, queries.q2) if q.volatile_functions]
    reference = ctx.case.meta.get("veriEQL")
    return compare(r1, r2, flags, dict(reference) if reference else None)


def comparison_detail(result: Comparison) -> dict:
    r1, r2 = result.q1, result.q2
    detail = dict(
        outcome=result.outcome, kind=result.kind, reason=result.reason,
        counterexample=result.counterexample, not_deterministic=list(result.not_deterministic),
        q1={"rows": len(r1.rows), "columns": list(r1.columns), "types": list(r1.types), "error": r1.error},
        q2={"rows": len(r2.rows), "columns": list(r2.columns), "types": list(r2.types), "error": r2.error},
        only_in_q1=[{"row": display(list(row)), "extra_copies": n} for row, n in result.only_in_q1],
        only_in_q2=[{"row": display(list(row)), "extra_copies": n} for row, n in result.only_in_q2],
        notes=list(result.notes),
    )
    if result.reference:
        detail["veriEQL_verdict"] = result.reference.get("verdict")
        detail["agrees_with_veriEQL"] = agreement(result.outcome, result.reference.get("verdict"))
    return detail


def compare_queries(ctx: "CaseContext") -> None:
    with ctx.journal.timed("compare", "run Q1 and Q2 on the loaded data, compare") as d:
        result = run_comparison(ctx)
        ctx.state["comparison"] = result
        d.update(comparison_detail(result))


def agreement(outcome: str, verdict: str | None) -> str:
    """How our outcome relates to VeriEQL's verdict for the pair."""
    if verdict is None:
        return "no reference"
    if outcome == DIFFER:
        return "agree (both found a difference)" if verdict == "different" else \
            f"we found a difference, VeriEQL says {verdict}"
    if verdict == "different":
        return "VeriEQL found a difference; not yet with this data (the mutation stage searches further)"
    return f"no difference yet; VeriEQL says {verdict}"
