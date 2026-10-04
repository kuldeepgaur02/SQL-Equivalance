"""Working with the rows a rebuild changes.

A rebuild starts from the rows actually in the database (as Step 6 stored
them), changes some values, and becomes a new base: one row per table, FKs
filled exactly as Step 5 fills them.
"""
from __future__ import annotations

from typing import Any

from ..basedata.builder import fill_foreign_keys
from ..basedata.model import BaseData, Cell
from ..insertion.model import LoadResult
from ..ordering.model import DEFERRED, PARENT, InsertPlan
from ..ordering.planner import root_table
from ..schema.model import SchemaModel
from ..schema.names import QName

CURRENT = "current"              # value kept from the rows in the database
REBUILD_RULE = "rebuild-rule"    # set from a query's filter by the rule table
REBUILD_LLM = "rebuild-llm"      # set by the LLM
MOVED_WITH_FK = "rebuild-fk"     # a parent key moved so the child's FK value stays valid

Rows = dict[QName, dict[str, Cell]]


def current_rows(model: SchemaModel, plan: InsertPlan, base: BaseData, load: LoadResult) -> Rows:
    """The base row of each planned table: as stored in the database, else as it was planned."""
    rows: Rows = {}
    for name in plan.order:
        insertable = [c.name for c in model.tables[name].insertable_columns]
        stored = load.rows.get(name)
        if stored is not None:
            rows[name] = {c: Cell(stored.get(c), CURRENT) for c in insertable}
        elif name in base.rows:
            rows[name] = {c: Cell(base.rows[name][c].value, CURRENT) for c in insertable if c in base.rows[name]}
    return rows


def values(rows: Rows) -> dict[str, dict[str, Any]]:
    return {str(t): {c: cell.value for c, cell in row.items()} for t, row in rows.items()}


def propagate_fk_values(model: SchemaModel, plan: InsertPlan, rows: Rows, changed: set[tuple[QName, str]]) -> None:
    """A filter like orders.user_id = 7 must move the parent key (users.id) to 7 too:
    FK columns are copied from the parent when the rows are inserted."""
    moved = True
    while moved:
        moved = False
        for p in plan.fks:
            if p.strategy not in (PARENT, DEFERRED) or p.fk.table not in rows:
                continue
            parent = root_table(model, p.fk.ref_table)
            if parent not in rows:
                continue
            for c, rc in zip(p.fk.columns, p.fk.ref_columns):
                if (p.fk.table, c) in changed and (parent, rc) not in changed:
                    value = rows[p.fk.table][c].value
                    if rows[parent][rc].value != value:
                        rows[parent][rc] = Cell(value, MOVED_WITH_FK, f"to keep {p.fk.name} valid")
                        changed.add((parent, rc))
                        moved = True


def to_base(model: SchemaModel, plan: InsertPlan, rows: Rows, warnings: list[str]) -> BaseData:
    updates = fill_foreign_keys(model, plan, rows)
    return BaseData(rows=rows, updates=tuple(updates), warnings=tuple(warnings))
