"""Readable summaries of what Step 3 found (CLI, journals, LLM prompts)."""
from __future__ import annotations

from typing import Any

from .model import Constant, Predicate, QueryInfo, QueryPair
from .rules import Rule


def describe_predicate(p: Predicate) -> str:
    term = p.term
    target = str(term.column)
    if term.json_path:
        target += "".join(f"->{k!r}" for k in term.json_path)
    if term.function:
        args = ", ".join(map(repr, term.function_args))
        target = f"{term.function}({target}{', ' + args if args else ''})"
    if term.cast:
        target = f"({target})::{term.cast}"
    values = ", ".join(_const(v) for v in p.values)
    op = {"is_null": "IS NULL", "is_true": "IS TRUE", "is_false": "IS FALSE"}.get(p.op, p.op.upper())
    text = f"{target} {op}" + (f" ({values})" if p.op in ("in", "between") else f" {values}" if values else "")
    if p.negated:
        text = f"NOT ({text})"
    return text


def describe_query(q: QueryInfo) -> str:
    lines = [f"{q.label}: " + ("parsed" if q.parsed else "NOT parsed by sqlglot (filters unknown)")]
    if q.result:
        lines.append("  returns:   " + ", ".join(f"{r.name} {r.type}" for r in q.result))
    if q.tables:
        lines.append("  reads:     " + ", ".join(map(str, q.tables))
                     + (f"  (through views {', '.join(map(str, q.views))})" if q.views else ""))
    for j in q.joins:
        lines.append(f"  join:      {j.kind} {j.target} ON {j.condition or '-'}  [{j.scope}]")
    for p in q.predicates:
        tag = "required" if p.required else "optional"
        lines.append(f"  filter:    {describe_predicate(p):55} [{p.clause}, {p.scope}, {tag}]")
    for c in q.comparisons:
        lines.append(f"  compare:   {c.sql:55} [{c.clause}, {c.scope}, {'required' if c.required else 'optional'}]")
    for u in q.unparsed:
        lines.append(f"  other:     {u}")
    if q.json_paths:
        lines.append("  json:      " + "; ".join(f"{col}: {', '.join('->'.join(map(str, p)) for p in paths)}"
                                             for col, paths in q.json_paths.items()))
    if q.features:
        lines.append("  features:  " + ", ".join(q.features))
    if q.aggregates:
        lines.append("  aggregates: " + ", ".join(q.aggregates))
    for w in q.warnings:
        lines.append(f"  warning:   {w}")
    return "\n".join(lines)


def describe_pair(pair: QueryPair) -> str:
    out = [describe_query(pair.q1), "", describe_query(pair.q2)]
    if pair.notes:
        out += [""] + [f"note: {n}" for n in pair.notes]
    return "\n".join(out)


def describe_rules(rules: dict[Any, tuple[Rule, ...]]) -> str:
    lines = []
    for table, rs in rules.items():
        for r in rs:
            state = "understood" if r.understood else "left to repair"
            parts = [describe_predicate(p) for p in r.predicates] + [c.sql for c in r.comparisons] + list(r.unparsed)
            lines.append(f"{table} {r.kind} {r.name}: {r.expression}\n    -> {state}: {'; '.join(parts) or '-'}")
    return "\n".join(lines)


def _const(c: Constant) -> str:
    return c.sql
