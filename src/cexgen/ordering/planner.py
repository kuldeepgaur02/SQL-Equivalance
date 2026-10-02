"""Step 4 — Table order (FK graph): build the insert plan.

  1. Which tables get rows: the tables Q1 / Q2 read (views expanded, partitions
     mapped to their partitioned parent) plus every FK parent they need,
     transitively. Other tables are skipped as not needed.
  2. FKs into a table that cannot get rows: NULL if the FK allows it, else the
     child is skipped too (and so on, transitively).
  3. Self-references: nullable -> insert NULL, then UPDATE. NOT NULL -> the row
     points at itself, unless a CHECK forbids that (then the table is skipped).
  4. Cycles: broken at a nullable FK (insert NULL, then UPDATE) where possible.
     A cycle of NOT NULL FKs becomes one step with its FKs deferred to the end
     of the step's transaction (made DEFERRABLE in the sandbox if they are not).
  5. Everything else: parents before children, ties broken by name.
"""
from __future__ import annotations

from typing import Mapping

from ..queries.model import QueryPair
from ..queries.rules import Rule
from ..schema.model import ForeignKey, SchemaModel, Table
from ..schema.names import QName
from .graph import Edge, FkGraph, strongly_connected
from .model import DEFERRED, NULL, NULL_THEN_UPDATE, PARENT, SELF, FkPlan, InsertPlan, Step

NOT_NEEDED = "not read by Q1 or Q2, and not an FK parent of a table they read"


def plan_inserts(model: SchemaModel, queries: QueryPair | None = None,
                 rules: Mapping[QName, tuple[Rule, ...]] | None = None, scope: str = "queries") -> InsertPlan:
    rules = rules or {}
    warnings: list[str] = []
    skipped: dict[QName, str] = {}
    targets = {t.qname for t in model.insert_targets}

    for t in model.insert_targets:
        if t.partitioning is not None and not t.partitioning.partitions:
            skipped[t.qname] = "partitioned table with no partitions: it cannot hold rows"

    # 1. which tables get rows ---------------------------------------------------
    if scope == "queries" and queries is not None and queries.q1.parsed and queries.q2.parsed:
        start: set[QName] = set()
        for q in (queries.q1, queries.q2):
            for t in q.tables:
                root = root_table(model, t)
                if root in targets:
                    start.add(root)
                else:
                    warnings.append(f"{q.label} reads {t}, which cannot hold test data")
        needed = _with_fk_parents(model, start, targets)
    else:
        if scope == "queries":
            warnings.append("a query could not be fully parsed, so every table gets a row")
        scope = "all"
        needed = set(targets)
    for t in sorted(targets - needed):
        skipped.setdefault(t, NOT_NEEDED)
    live = needed - set(skipped)

    # 2 + 3. FKs into tables without rows, and self-references (repeat: skipping cascades)
    plans: dict[tuple[QName, str], FkPlan] = {}
    changed = True
    while changed:
        changed = False
        for name in sorted(live):
            table = model.tables[name]
            reason = _settle_table(model, table, live, skipped, rules, plans)
            if reason:
                skipped[name] = reason
                live.discard(name)
                for key in [k for k in plans if k[0] == name]:
                    del plans[key]
                changed = True

    # 4. cycles ------------------------------------------------------------------------
    edges = [Edge(fk.table, root_table(model, fk.ref_table), fk)
             for name in sorted(live) for fk in model.tables[name].foreign_keys
             if not fk.self_reference and root_table(model, fk.ref_table) in live]
    edges = [e for e in edges if e.child != e.parent]       # FK to own partition root acts like a self-reference
    while True:
        cycles = [c for c in strongly_connected(sorted(live), _successors(live, edges)) if len(c) > 1]
        broke = False
        for comp in sorted(cycles, key=min):
            inner = [e for e in edges if e.child in comp and e.parent in comp]
            nullable = [e for e in inner if model.tables[e.child].fk_can_be_null(e.fk)]
            if nullable:
                e = nullable[0]
                edges.remove(e)
                cycle = " -> ".join(str(t) for t in sorted(comp))
                plans[(e.child, e.fk.name)] = FkPlan(e.fk, NULL_THEN_UPDATE,
                                                     f"breaks the cycle {cycle}: insert NULL, then UPDATE",
                                                     _null_columns(model.tables[e.child], e.fk))
                broke = True
        if not broke:
            break

    # 5. steps ---------------------------------------------------------------------------
    graph = FkGraph(live, edges)
    steps: list[Step] = []
    make_deferrable: list[ForeignKey] = []
    for comp in graph.ordered_components():
        inner = [e for e in graph.edges if e.child in comp and e.parent in comp]
        if len(comp) > 1:
            for e in inner:
                plans[(e.child, e.fk.name)] = FkPlan(
                    e.fk, DEFERRED, f"every FK in the cycle {' <-> '.join(map(str, comp))} is NOT NULL: "
                                    f"rows go in together, the FK is checked at the end of the transaction")
                if not e.fk.deferrable:
                    make_deferrable.append(e.fk)
        steps.append(Step(tuple(comp), tuple(e.fk for e in inner)))
    for e in graph.edges:
        plans.setdefault((e.child, e.fk.name), FkPlan(e.fk, PARENT, f"{e.parent} is inserted first"))

    for name in sorted(live):
        for trg in model.tables[name].triggers:
            if trg.enabled and "insert" in trg.events:
                warnings.append(f"inserting into {name} fires trigger {trg.name}, which may change other rows")

    fk_list = tuple(sorted(plans.values(), key=lambda p: (p.fk.table, p.fk.name)))
    return InsertPlan(
        steps=tuple(steps), fks=fk_list, updates=tuple(p for p in fk_list if p.strategy == NULL_THEN_UPDATE),
        skipped=dict(sorted(skipped.items())), make_deferrable=tuple(make_deferrable), scope=scope,
        warnings=tuple(warnings))


def root_table(model: SchemaModel, name: QName) -> QName:
    """A partition's rows go in through its top-level partitioned table."""
    seen = set()
    while name in model.tables and model.tables[name].partition_of is not None and name not in seen:
        seen.add(name)
        name = model.tables[name].partition_of
    return name


# -- helpers -------------------------------------------------------------------------
def _with_fk_parents(model: SchemaModel, start: set[QName], targets: set[QName]) -> set[QName]:
    needed, todo = set(start), sorted(start)
    while todo:
        t = todo.pop()
        for fk in model.tables[t].foreign_keys:
            parent = root_table(model, fk.ref_table)
            if parent in targets and parent not in needed:
                needed.add(parent)
                todo.append(parent)
    return needed


def _settle_table(model: SchemaModel, table: Table, live: set[QName], skipped: dict[QName, str],
                  rules: Mapping[QName, tuple[Rule, ...]], plans: dict) -> str | None:
    """Plan the table's FKs that do not need ordering. Returns a reason if the table must be skipped."""
    for fk in table.foreign_keys:
        key = (table.qname, fk.name)
        parent = root_table(model, fk.ref_table)
        if fk.self_reference or parent == table.qname:
            if table.fk_can_be_null(fk):
                plans[key] = FkPlan(fk, NULL_THEN_UPDATE, "self-reference: insert NULL first, then UPDATE",
                                    _null_columns(table, fk))
            elif _self_forbidden(table, fk, rules):
                return (f"NOT NULL self-reference {fk.name}, and a CHECK forbids a row from pointing at itself")
            else:
                plans[key] = FkPlan(fk, SELF, "NOT NULL self-reference: the row points at itself "
                                              "(Postgres checks the FK after the row is written)")
            continue
        if parent in live:
            continue
        why = skipped.get(parent, "it is not a table that can hold rows")
        if table.fk_can_be_null(fk):
            plans[key] = FkPlan(fk, NULL, f"{parent} gets no rows ({why})", _null_columns(table, fk))
        else:
            return f"NOT NULL foreign key {fk.name} needs a row in {parent}, which gets none ({why})"
    return None


def _self_forbidden(table: Table, fk: ForeignKey, rules: Mapping[QName, tuple[Rule, ...]]) -> bool:
    pairs = {frozenset((c, r)) for c, r in zip(fk.columns, fk.ref_columns)}
    for rule in rules.get(table.qname, ()):
        for cmp in rule.comparisons:
            if cmp.required and cmp.op in ("<>", "<", ">") and cmp.left.is_plain and cmp.right.is_plain \
                    and frozenset((cmp.left.column.column, cmp.right.column.column)) in pairs:
                return True
    return False


def _null_columns(table: Table, fk: ForeignKey) -> tuple[str, ...]:
    """MATCH FULL: every column NULL. MATCH SIMPLE: the nullable ones (one is enough)."""
    if fk.match == "full":
        return fk.columns
    return tuple(c for c in fk.columns if table.columns[c].nullable)


def _successors(live: set[QName], edges: list[Edge]) -> dict[QName, list[QName]]:
    out: dict[QName, list[QName]] = {t: [] for t in sorted(live)}
    for e in edges:
        if e.parent not in out[e.child]:
            out[e.child].append(e.parent)
    for k in out:
        out[k].sort()
    return out
