"""Readable form of the load result."""
from __future__ import annotations

import json

from ..journal.entry import to_jsonable
from .model import LoadResult


def describe_load(result: LoadResult) -> str:
    lines = [f"loaded: {len(result.rows)} table(s) got their base row; {result.total_rows} row(s) in the database"]
    for table, row in result.rows.items():
        lines.append(f"  {table}: {json.dumps(to_jsonable(row), ensure_ascii=False)}")
    for r in result.repairs:
        changed = ", ".join(f"{c}: {json.dumps(to_jsonable(a))} -> {json.dumps(to_jsonable(b))}"
                            for c, (a, b) in r.changed.items())
        how = r.method or "no fix found"
        lines.append(f"  repair {r.table} #{r.attempt} [{r.category} {r.sqlstate} {r.constraint or ''}] "
                     f"by {how}: {changed or '-'}" + (f"  ({r.note})" if r.note else ""))
    for u in result.updates:
        lines.append(f"  UPDATE {u}: done")
    for u in result.failed_updates:
        lines.append(f"  UPDATE {u}")
    for t, why in result.skipped.items():
        lines.append(f"  skipped {t}: {why}")
    for t, rows in result.snapshot.items():
        if t not in result.rows or len(rows) > 1:
            lines.append(f"  {t} holds {len(rows)} row(s) in total (seed rows or rows added by triggers)")
    for w in result.warnings:
        lines.append(f"  warning: {w}")
    return "\n".join(lines)
