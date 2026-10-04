"""Step 9 — the hand-off: everything the mutation stage needs, in one JSON document.

The diagram's MUTATION STAGE box: "LLM is primary — gets schema, both queries,
current data, and every attempt already made with its exact result". So:

    status      counterexample (rows / order) | counterexample_error (one query fails) |
                column_order_only (same values, other column order: not counted, handed on) |
                counterexample_untrusted (not deterministic) | to_mutation
    schema      the SQL as given, and per table: columns, keys, FKs, CHECKs
    queries     Q1, Q2, and the filters Step 3 found in each
    data        every row in the database now, where each base value came from,
                and a replay script that recreates exactly this database
    result      the final comparison (with the rows only in Q1 / only in Q2)
    attempts    every round (the base, then each rebuild): rows planned, rows stored,
                repairs, and the comparison it gave
    mutation    the actions the diagram lists for the next stage
"""
from __future__ import annotations

from typing import Any

from ..compare.model import BOTH_EMPTY, COLUMNS, DIFFER, ERROR, Comparison
from ..compare.normalize import display
from ..compare.step import comparison_detail
from ..journal.entry import to_jsonable
from ..queries.describe import describe_predicate
from ..rebuild.model import Round
from ..schema.model import SchemaModel, Table

FORMAT = "cexgen-handoff/1"
MUTATION_ACTIONS = ["set_null", "set_value", "add_row", "delete_row", "duplicate", "empty_table"]
RESULT_ROWS = 50                                     # rows of each query's result kept in the hand-off

COUNTEREXAMPLE = "counterexample"                    # different rows (or order): stop here
COUNTEREXAMPLE_ERROR = "counterexample_error"        # one query fails on this data, the other does not
COLUMN_ORDER_ONLY = "column_order_only"              # the same values in another column order: not counted;
                                                     # still handed to the mutation stage
UNTRUSTED = "counterexample_untrusted"               # they differ, but the result is not deterministic
TO_MUTATION = "to_mutation"                          # no difference yet: the mutation stage searches further
HANDED_TO_MUTATION = (TO_MUTATION, COLUMN_ORDER_ONLY)


def status_of(comparison: Comparison) -> str:
    if comparison.outcome != DIFFER:
        return TO_MUTATION
    if comparison.not_deterministic:
        return UNTRUSTED
    if comparison.kind == COLUMNS:
        return COLUMN_ORDER_ONLY
    if comparison.kind == ERROR:
        return COUNTEREXAMPLE_ERROR
    return COUNTEREXAMPLE


def build_handoff(ctx, data_script: str | None, verified: tuple[bool | None, str] | None) -> dict[str, Any]:
    state, case = ctx.state, ctx.case
    model: SchemaModel = state["schema"]
    comparison: Comparison = state["comparison"]
    rounds: list[Round] = state.get("rounds") or []
    queries = state.get("queries")
    load = state["load"]
    status = status_of(comparison)

    doc: dict[str, Any] = {
        "format": FORMAT,
        "status": status,
        "starting_point": (comparison.outcome if status == TO_MUTATION else          # same | both_empty
                           "column_order_only" if status == COLUMN_ORDER_ONLY else None),
        "case": {"name": case.name, "source": case.source, "meta": dict(case.meta)},
        "schema": {"fingerprint": ctx.workspace.fingerprint, "sql": case.schema_sql,
                   "tables": {str(t.qname): _table(t) for t in model.tables.values() if t.insert_target}},
        "queries": {"q1": case.q1, "q2": case.q2},
        "result": {**comparison_detail(comparison),
                   "q1_rows": [display(list(r)) for r in comparison.q1.rows[:RESULT_ROWS]],
                   "q2_rows": [display(list(r)) for r in comparison.q2.rows[:RESULT_ROWS]]},
        "data": {
            "tables": {str(t): {"columns": list(rows[0].keys()) if rows else [],
                                "rows": [display(list(r.values())) for r in rows]}
                       for t, rows in load.snapshot.items()},
            "base_row_sources": {str(t): {c: cell.source for c, cell in row.items()}
                                 for t, row in state["base"].rows.items()},
            "replay_sql": data_script,
            "replay_verified": verified[0] if verified else None,
            "replay_note": verified[1] if verified else "not verified",
        },
        "attempts": [_round(r) for r in rounds],
        "plan": {"insert_order": [str(t) for t in state["plan"].order],
                 "foreign_keys": {f"{p.fk.table}.{p.fk.name}": p.strategy for p in state["plan"].fks},
                 "skipped": {str(t): why for t, why in state["plan"].skipped.items()}},
        "flags": {"not_deterministic": list(comparison.not_deterministic),
                  "identical_queries": case.identical_queries,
                  "both_empty_after_rebuilds": comparison.outcome == BOTH_EMPTY,
                  "not_enforced_constraints": list(case.meta.get("not_enforced", []))},
        "mutation": {"actions": MUTATION_ACTIONS,
                     "note": "apply actions on a copy of this data; the schema's rules must keep holding"},
        "llm": {"mode": "llm" if ctx.settings.llm_enabled else "rules",
                "calls": getattr(state.get("oracle"), "calls", 0)},
        "journal": str(ctx.journal.path) if ctx.journal.path else None,
    }
    if queries is not None:
        doc["queries"].update({
            f"{q.label.lower()}_analysis": {
                "parsed": q.parsed,
                "filters": [describe_predicate(p) + ("" if p.required else " (optional)") for p in q.predicates],
                "joins": [c.sql for c in q.comparisons], "other_conditions": list(q.unparsed),
                "features": list(q.features), "result_columns": [f"{r.name} {r.type}" for r in q.result],
            } for q in (queries.q1, queries.q2)})
        doc["queries"]["notes"] = list(queries.notes)
    return to_jsonable(doc)


def _table(t: Table) -> dict[str, Any]:
    return {
        "columns": [{"name": c.name, "type": c.type.sql, "nullable": c.nullable,
                     "default": c.default, "identity": c.identity, "generated": c.generated,
                     **({"enum": list(c.type.enum_labels)} if c.type.enum_labels else {})}
                    for c in t.columns.values()],
        "primary_key": list(t.primary_key.columns) if t.primary_key else [],
        "unique": [list(u.elements) + ([f"WHERE {u.predicate}"] if u.predicate else []) for u in t.unique_keys],
        "foreign_keys": [{"name": f.name, "columns": list(f.columns), "references": str(f.ref_table),
                          "ref_columns": list(f.ref_columns)} for f in t.foreign_keys],
        "checks": [c.expression for c in t.all_checks],
    }


def _round(r: Round) -> dict[str, Any]:
    return {
        "round": r.number, "method": r.method, "changes": list(r.changes), "notes": list(r.notes),
        "rows_planned": {str(t): {c: cell.value for c, cell in row.items()} for t, row in r.base.rows.items()},
        "rows_stored": {str(t): row for t, row in r.load.rows.items()},
        "skipped": {str(t): why for t, why in r.load.skipped.items()},
        "repairs": [{"table": str(x.table), "attempt": x.attempt, "category": x.category, "sqlstate": x.sqlstate,
                     "constraint": x.constraint, "method": x.method,
                     "changed": {c: [a, b] for c, (a, b) in x.changed.items()}} for x in r.load.repairs],
        "comparison": comparison_detail(r.comparison),
    }
