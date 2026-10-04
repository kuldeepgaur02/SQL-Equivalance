"""Step 8 — the diagram's loop: both results empty -> rebuild the base -> INSERT -> compare.

    while both queries return no rows, at most max_rebuilds times:
        new rows   LLM (rule table with --no-llm, or if the call fails / changes nothing usable)
        reset the database to the schema's initial state
        INSERT with the repair loop (Step 6), compare (Step 7)
    stop as soon as a result is not empty (differ or same), or when nothing new can be tried.

Every round is kept (ctx.state["rounds"]) with its exact result.
"""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from ..compare.model import BOTH_EMPTY
from ..compare.step import comparison_detail, run_comparison
from ..insertion.loader import Loader
from ..journal.entry import to_jsonable
from ..llm.oracle import Oracle
from .llm import llm_rebuild
from .model import Round
from .rows import current_rows, to_base, values
from .rules import rule_rebuild

if TYPE_CHECKING:
    from ..runner.batch import CaseContext

log = logging.getLogger(__name__)


def rebuild_until_rows(ctx: "CaseContext") -> None:
    state = ctx.state
    rounds: list[Round] = state.setdefault("rounds", [Round(0, "base", state["base"], state["load"],
                                                            state["comparison"])])
    if state["comparison"].outcome != BOTH_EMPTY or state.get("queries") is None:
        return
    model, plan, queries = state["schema"], state["plan"], state["queries"]
    oracle = state.get("oracle") or Oracle(ctx.settings, ctx.journal, ctx.memory)
    state["oracle"] = oracle
    tried: list[str] = []

    for number in range(1, ctx.settings.max_rebuilds + 1):
        last = rounds[-1]
        rows = current_rows(model, plan, last.base, last.load)
        history = [{"round": r.number, "rows": json.dumps(to_jsonable(values(current_rows(model, plan, r.base, r.load)))),
                    "q1": _result(r.comparison.q1), "q2": _result(r.comparison.q2)} for r in rounds[1:]]
        log.info("both results empty: rebuild round %d of %d (%s)", number, ctx.settings.max_rebuilds, oracle.mode)
        method, notes = "rules", []
        if oracle.mode == "llm":
            new, changes, note = llm_rebuild(oracle, model, plan, queries, rows, history, number)
            method = "llm"
            if new is None:
                notes.append(note)
                new, changes, more = rule_rebuild(model, queries, state.get("rules"), plan, rows, number)
                method, notes = "rules (llm failed)", notes + more
            elif note:
                notes.append(note)
        else:
            new, changes, notes = rule_rebuild(model, queries, state.get("rules"), plan, rows, number)

        signature = json.dumps(to_jsonable(values(new)), sort_keys=True)
        if not changes or signature in tried:
            ctx.journal.record("rebuild", f"round {number}: nothing new to try", "skipped",
                               detail={"method": method, "notes": notes})
            if method.startswith("rules") and number < ctx.settings.max_rebuilds:
                continue                       # the next round uses other filters (Q2, then both)
            break
        tried.append(signature)

        with ctx.journal.timed("rebuild", f"round {number}: rebuild the base ({method}), INSERT, compare") as d:
            base = to_base(model, plan, new, notes)
            ctx.workspace.reset()
            load = Loader(ctx.workspace, model, plan, base, state.get("rules"), oracle, ctx.journal, ctx.memory,
                          remember_rows=False).load()
            comparison = run_comparison(ctx)
            d.update(method=method, changes=changes, notes=notes,
                     skipped={str(t): why for t, why in load.skipped.items()},
                     repairs=len(load.repairs), comparison=comparison_detail(comparison))
        rounds.append(Round(number, method, base, load, comparison, tuple(changes), tuple(notes)))
        state.update(base=base, load=load, comparison=comparison)
        if comparison.outcome != BOTH_EMPTY:
            return


def _result(run) -> str:
    return f"error {run.error['sqlstate']}" if run.error else f"{len(run.rows)} row(s)"
