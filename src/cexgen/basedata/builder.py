"""Step 5 — Base data: one row per table in the insert plan.

  1. every insertable column gets a value:
       a row known to insert cleanly (schema memory), else
       scalar -> hardcoded default (ignores the queries), ENUM -> first label;
  2. the row is made to satisfy the schema's understood rules and partition bounds;
  3. JSON / ARRAY columns -> the oracle (LLM, or the rule table with --no-llm),
     built from the paths the queries read;
  4. foreign keys are filled as the insert plan says (parent / self / deferred /
     NULL / NULL now and UPDATE later).
"""
from __future__ import annotations

from typing import Any, Mapping

from ..llm.oracle import Oracle
from ..ordering.model import DEFERRED, NULL, NULL_THEN_UPDATE, PARENT, SELF, InsertPlan
from ..ordering.planner import root_table
from ..queries.model import QueryPair
from ..queries.rules import Rule
from ..schema.model import Column, SchemaModel
from ..schema.names import QName
from ..schema.types import Family
from .defaults import default_value
from .json_array import json_array_values
from .model import DEFAULT, ENUM, FK, FK_NULL, MEMORY, RULE, BaseData, Cell, PendingUpdate
from .solver import place_in_partition, solve_row
from .values import coerce

MEMORY_NAMESPACE = "base_rows"     # Step 6 stores rows that inserted cleanly here


def build_base(model: SchemaModel, plan: InsertPlan, rules: Mapping[QName, tuple[Rule, ...]] | None,
               queries: QueryPair | None, oracle: Oracle) -> BaseData:
    rules = rules or {}
    warnings: list[str] = []
    unsatisfied: list[str] = []
    rows: dict[QName, dict[str, Cell]] = {}
    fk_columns = {(p.fk.table, c) for p in plan.fks for c in p.fk.columns}

    # 1 + 2: values, then the schema's rules -------------------------------------------
    for name in plan.order:
        table = model.tables[name]
        remembered = oracle.memory.get(MEMORY_NAMESPACE, str(name))
        if isinstance(remembered, dict) and set(remembered) == {c.name for c in table.insertable_columns}:
            cells = {c.name: Cell(coerce(remembered[c.name], c), MEMORY) for c in table.insertable_columns}
        else:
            cells = {c.name: Cell(default_value(c), ENUM if c.type.family == Family.ENUM else DEFAULT)
                     for c in table.insertable_columns}
            for c in table.insertable_columns:
                if c.type.family == Family.ENUM and not c.type.enum_labels:
                    warnings.append(f"{name}.{c.name}: enum {c.type.name} has no labels, so the only value is NULL")

        values = {k: cell.value for k, cell in cells.items()}
        problems = solve_row(table, values, rules.get(name, ()))
        problem = place_in_partition(model, table, values)
        if problem:
            problems.append(problem)
        for col, value in values.items():
            if value != cells[col].value or type(value) is not type(cells[col].value):
                cells[col] = Cell(value, RULE)
        unsatisfied += [f"{name}: {p}" for p in problems]
        rows[name] = cells

    # 3: JSON / ARRAY ---------------------------------------------------------------------
    wanted = [(name, c) for name in plan.order for c in model.tables[name].insertable_columns
              if c.type.family in (Family.JSON, Family.ARRAY) and (name, c.name) not in fk_columns
              and rows[name][c.name].source != MEMORY]
    if wanted:
        for (name, col), answer in json_array_values(wanted, model, queries, oracle).items():
            rows[name][col] = Cell(answer.value, answer.source, answer.note)

    # 4: foreign keys, in plan order (a parent's key is final before its children copy it)
    updates: list[PendingUpdate] = []
    for name in plan.order:
        for fkp in plan.fks_of(name):
            fk, row = fkp.fk, rows[name]
            source_row = row if fkp.strategy == SELF or fk.self_reference else rows.get(root_table(model, fk.ref_table))
            target = {c: (source_row[rc].value if source_row else None) for c, rc in zip(fk.columns, fk.ref_columns)}
            if fkp.strategy in (PARENT, DEFERRED, SELF):
                for c, v in target.items():
                    row[c] = Cell(v, FK, fk.name)
            elif fkp.strategy == NULL:
                for c in fkp.null_columns:
                    row[c] = Cell(None, FK_NULL, f"{fk.name}: {fkp.reason}")
            elif fkp.strategy == NULL_THEN_UPDATE:
                for c in fkp.null_columns:
                    row[c] = Cell(None, FK_NULL, f"{fk.name}: set by UPDATE after all rows exist")
                for c in fk.columns:
                    if c not in fkp.null_columns:
                        row[c] = Cell(target[c], FK, fk.name)
                updates.append(PendingUpdate(name, fk, target))

    return BaseData(rows=rows, updates=tuple(updates), unsatisfied=tuple(unsatisfied), warnings=tuple(warnings))


def cell_values(cells: Mapping[str, Cell]) -> dict[str, Any]:
    return {k: c.value for k, c in cells.items()}


__all__ = ["build_base", "cell_values", "Column"]
