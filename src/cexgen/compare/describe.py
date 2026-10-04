"""Readable form of a comparison."""
from __future__ import annotations

import json

from .model import Comparison, QueryRun
from .normalize import display
from .step import agreement


def describe_comparison(c: Comparison) -> str:
    head = c.outcome.upper().replace("_", " ") + (f" ({c.kind})" if c.kind else "")
    lines = [f"{head}: {c.reason}"]
    for r in (c.q1, c.q2):
        lines.append("  " + _run(r))
    for label, rows in (("only in Q1", c.only_in_q1), ("only in Q2", c.only_in_q2)):
        for row, n in rows:
            lines.append(f"  {label}: {json.dumps(display(list(row)), ensure_ascii=False)}" + (f"  x{n}" if n > 1 else ""))
    for n in c.notes:
        lines.append(f"  note: {n}")
    for n in c.not_deterministic:
        lines.append(f"  NOT DETERMINISTIC: {n}")
    if c.outcome == "differ":
        lines.append("  counterexample: " + ("yes" if c.counterexample else "not trusted (result not deterministic)"))
    if c.reference:
        lines.append(f"  VeriEQL: {c.reference.get('verdict')} -> {agreement(c.outcome, c.reference.get('verdict'))}")
    return "\n".join(lines)


def _run(r: QueryRun) -> str:
    if r.error:
        return f"{r.label}: ERROR {r.error['sqlstate']}: {r.error['message']}"
    cols = ", ".join(f"{n} {t}" for n, t in zip(r.columns, r.types))
    order = ", ordered" if r.ordered else ""
    return f"{r.label}: {len(r.rows)} row(s){order} [{cols}] in {r.duration_ms:.1f} ms"
