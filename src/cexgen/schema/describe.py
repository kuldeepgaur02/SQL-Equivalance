"""Readable and JSON forms of the schema model (for the CLI, journals and LLM prompts)."""
from __future__ import annotations

import dataclasses
from typing import Any, Mapping

from ..sqltext.names import quote_ident
from .model import SchemaModel, Table
from .names import QName
from .types import TypeInfo


def describe(model: SchemaModel) -> str:
    lines = [f"PostgreSQL {model.server_version // 10000}, search_path: {', '.join(model.search_path)}"]
    for t in model.tables.values():
        lines.append("")
        lines += describe_table(t)
    for v in model.views.values():
        lines.append("")
        lines.append(f"{v.kind.upper()} {v.qname}  reads: {', '.join(map(str, v.reads)) or '(nothing)'}")
        for c in v.columns.values():
            lines.append(f"  {quote_ident(c.name)} {c.type.sql}")
    if model.warnings:
        lines += ["", "WARNINGS"] + [f"  - {w}" for w in model.warnings]
    return "\n".join(lines)


def describe_table(t: Table) -> list[str]:
    head = f"{t.kind.upper()} {t.qname}"
    if t.partition_of:
        head += f"  partition of {t.partition_of} {t.partition_bound.text}"
    if t.inherits:
        head += f"  inherits {', '.join(map(str, t.inherits))}"
    if t.seed_rows:
        head += f"  [{t.seed_rows} seed row(s)]"
    out = [head]
    if t.kind == "partition" and t.partitioning is None:
        return out                                   # same columns and rules as its parent
    pk = set(t.primary_key.columns) if t.primary_key else set()
    for c in t.columns.values():
        bits = [quote_ident(c.name), c.type.sql, "NULL" if c.nullable else "NOT NULL"]
        if c.name in pk:
            bits.append("PK")
        if c.identity:
            bits.append(f"IDENTITY {c.identity.upper()}")
        elif c.default is not None:
            bits.append(f"DEFAULT {c.default}")
        if c.generated:
            bits.append(f"GENERATED AS ({c.generated})")
        detail = _type_detail(c.type)
        if detail:
            bits.append(f"[{detail}]")
        out.append("  " + " ".join(bits))

    if t.primary_key:
        out.append(f"  entity:      PRIMARY KEY ({', '.join(t.primary_key.columns)})")
    for u in t.unique_keys:
        extra = (" WHERE " + u.predicate if u.predicate else "") + (" NULLS NOT DISTINCT" if u.nulls_not_distinct else "")
        kind = "UNIQUE" if u.is_constraint else "UNIQUE INDEX"
        out.append(f"  entity:      {kind} {u.name} ({', '.join(u.elements)}){extra}")
    for fk in t.foreign_keys:
        extra = [f"MATCH {fk.match.upper()}"] if fk.match != "simple" else []
        extra += [f"ON DELETE {fk.on_delete.upper()}"] if fk.on_delete != "no action" else []
        extra += ["DEFERRABLE"] if fk.deferrable else []
        extra += ["NOT VALID"] if not fk.validated else []
        out.append(f"  referential: {fk.name} ({', '.join(fk.columns)}) -> {fk.ref_table} "
                   f"({', '.join(fk.ref_columns)}) {' '.join(extra)}".rstrip())
    for ch in t.all_checks:
        origin = f" (domain {ch.domain})" if ch.origin == "domain" else ""
        out.append(f"  general:     CHECK {ch.name}: {ch.expression}{origin}")
    for ex in t.exclusions:
        out.append(f"  general:     {ex.definition}")
    if t.partitioning:
        p = t.partitioning
        out.append(f"  general:     PARTITION BY {p.key_sql}; rows must fit one of:")
        out += [f"                 {part.table} {part.bound.text}" for part in p.partitions] or ["                 (no partitions)"]
    for trg in t.triggers:
        out.append(f"  behaviour:   trigger {trg.name} {trg.timing} {'/'.join(trg.events)} for each {trg.level}"
                   + ("" if trg.enabled else " (disabled)"))
    for rule in t.rules:
        out.append(f"  behaviour:   rule {rule}")
    if t.force_row_security:
        out.append("  behaviour:   row-level security forced")
    return out


def _type_detail(t: TypeInfo) -> str:
    bits = []
    if t.enum_labels:
        bits.append("values " + ", ".join(repr(v) for v in t.enum_labels))
    if t.element is not None:
        bits.append(f"of {t.element.sql}" + (f" values {', '.join(map(repr, t.element.enum_labels))}"
                                              if t.element.enum_labels else ""))
    if t.domains:
        bits.append("domain over " + t.base)
    if t.family == "other":
        bits.append("no value rules")
    return "; ".join(bits)


def to_dict(obj: Any) -> Any:
    """JSON-ready form of the model (QName -> 'schema.name')."""
    if isinstance(obj, QName):
        return str(obj)
    if dataclasses.is_dataclass(obj):
        return {f.name: to_dict(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, Mapping):
        return {str(k): to_dict(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_dict(v) for v in obj]
    return obj
