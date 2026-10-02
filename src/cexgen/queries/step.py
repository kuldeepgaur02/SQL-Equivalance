"""Step 3 as a pipeline step: analyse Q1 and Q2, and break down the schema's rules.

The rules are analysed once per schema (cached on the workspace). The queries
are analysed per case. A query that Postgres rejects, or that writes, stops
the case with a QueryError.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from .analyzer import analyze_query, pair_notes
from .describe import describe_predicate
from .model import QueryPair
from .rules import table_rules

if TYPE_CHECKING:
    from ..runner.batch import CaseContext

RULES_KEY = "table_rules"


def parse_queries(ctx: "CaseContext") -> None:
    model = ctx.state["schema"]
    ws, j = ctx.workspace, ctx.journal

    rules = ws.cache.get(RULES_KEY)
    if rules is None:
        with j.timed("parse_queries", "break down the schema's CHECK and index rules") as d:
            rules = ws.cache[RULES_KEY] = table_rules(model)
            all_rules = [r for rs in rules.values() for r in rs]
            d.update(rules=len(all_rules), understood=sum(r.understood for r in all_rules),
                     left_to_repair=[f"{r.table} {r.name}: {r.expression}" for r in all_rules if not r.understood])
    ctx.state["rules"] = rules

    infos = {}
    for label, sql in (("Q1", ctx.case.q1), ("Q2", ctx.case.q2)):
        with j.timed("parse_queries", f"{label}: check with Postgres, analyse with sqlglot") as d:
            info = infos[label] = analyze_query(label, sql, model, ctx.conn)
            d.update(parsed=info.parsed, result=[f"{r.name} {r.type}" for r in info.result],
                     tables=[str(t) for t in info.tables],
                     filters=[describe_predicate(p) + ("" if p.required else "  (optional)") for p in info.predicates],
                     comparisons=[c.sql for c in info.comparisons], unparsed=list(info.unparsed),
                     features=list(info.features), warnings=list(info.warnings))
    pair = QueryPair(infos["Q1"], infos["Q2"], pair_notes(infos["Q1"], infos["Q2"], ctx.case.identical_queries))
    ctx.state["queries"] = pair
    for note in pair.notes:
        j.record("parse_queries", note, "info")
