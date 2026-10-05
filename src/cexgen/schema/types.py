"""What values a column may hold: the type family and its limits (domain integrity).

Every Postgres type is sorted into a family. Later steps generate values per
family, so a new type only needs a family, not new code everywhere.
"""
from __future__ import annotations

from dataclasses import dataclass

from .names import QName


class Family:
    INTEGER = "integer"
    NUMERIC = "numeric"
    FLOAT = "float"
    MONEY = "money"
    TEXT = "text"
    BOOLEAN = "boolean"
    DATE = "date"
    TIME = "time"
    TIMESTAMP = "timestamp"
    INTERVAL = "interval"
    UUID = "uuid"
    JSON = "json"
    BYTES = "bytes"
    ENUM = "enum"
    ARRAY = "array"
    RANGE = "range"
    NETWORK = "network"
    BIT = "bit"
    GEOMETRIC = "geometric"
    COMPOSITE = "composite"
    OTHER = "other"          # anything else: written as text, Postgres casts it


# base type name -> family
_FAMILY_BY_NAME = {
    "int2": Family.INTEGER, "int4": Family.INTEGER, "int8": Family.INTEGER,
    "numeric": Family.NUMERIC,
    "float4": Family.FLOAT, "float8": Family.FLOAT,
    "money": Family.MONEY,
    "text": Family.TEXT, "varchar": Family.TEXT, "bpchar": Family.TEXT, "name": Family.TEXT,
    "char": Family.TEXT, "citext": Family.TEXT,
    "bool": Family.BOOLEAN,
    "date": Family.DATE,
    "time": Family.TIME, "timetz": Family.TIME,
    "timestamp": Family.TIMESTAMP, "timestamptz": Family.TIMESTAMP,
    "interval": Family.INTERVAL,
    "uuid": Family.UUID,
    "json": Family.JSON, "jsonb": Family.JSON,
    "bytea": Family.BYTES,
    "inet": Family.NETWORK, "cidr": Family.NETWORK, "macaddr": Family.NETWORK, "macaddr8": Family.NETWORK,
    "bit": Family.BIT, "varbit": Family.BIT,
    "point": Family.GEOMETRIC, "line": Family.GEOMETRIC, "lseg": Family.GEOMETRIC, "box": Family.GEOMETRIC,
    "path": Family.GEOMETRIC, "polygon": Family.GEOMETRIC, "circle": Family.GEOMETRIC,
}

INTEGER_LIMITS = {
    "int2": (-32768, 32767),
    "int4": (-2147483648, 2147483647),
    "int8": (-9223372036854775808, 9223372036854775807),
}


@dataclass(frozen=True)
class DomainCheck:
    domain: QName
    name: str
    expression: str          # uses the keyword VALUE for the value being checked
    validated: bool


@dataclass(frozen=True)
class TypeInfo:
    name: QName                          # declared type (the domain, if it is one)
    sql: str                             # type as Postgres prints it, usable in a cast: character varying(40)
    base: str                            # base type name after unwrapping domains: varchar
    family: str                          # a Family value
    length: int | None = None            # varchar(n) / char(n) / bit(n)
    varying: bool | None = None          # varchar / varbit vs char / bit
    precision: int | None = None         # numeric(p, s) / time(p) / timestamp(p) / interval(p)
    scale: int | None = None             # numeric(p, s); can be negative from PG 15
    min_value: int | None = None         # integer families
    max_value: int | None = None
    with_time_zone: bool | None = None
    binary_json: bool | None = None      # jsonb (True) vs json (False)
    enum_labels: tuple[str, ...] = ()    # in sort order
    element: "TypeInfo | None" = None    # arrays: the element type
    dimensions: int = 0                  # arrays: declared dimensions (Postgres does not enforce them)
    subtype: "TypeInfo | None" = None    # ranges: the bound type
    multirange: bool = False
    fields: tuple[tuple[str, "TypeInfo"], ...] = ()   # composite types
    domains: tuple[QName, ...] = ()      # domain chain, outermost first
    domain_not_null: bool = False
    domain_checks: tuple[DomainCheck, ...] = ()
    extension: str | None = None         # extension that defines the base type (citext, hstore ...)


def family_of(base: str) -> str:
    return _FAMILY_BY_NAME.get(base, Family.OTHER)


def decode_typmod(base: str, typmod: int) -> dict:
    """Turn Postgres's packed type modifier into named limits."""
    out: dict = {}
    if base in INTEGER_LIMITS:
        out["min_value"], out["max_value"] = INTEGER_LIMITS[base]
    if base in ("varchar", "bpchar"):
        out["varying"] = base == "varchar"
        if typmod >= 4:
            out["length"] = typmod - 4
        elif base == "bpchar":
            out["length"] = 1                       # plain "char(n)" without n means char(1)
    elif base in ("bit", "varbit"):
        out["varying"] = base == "varbit"
        if typmod >= 0:
            out["length"] = typmod
        elif base == "bit":
            out["length"] = 1
    elif base == "numeric" and typmod >= 4:
        mod = typmod - 4
        out["precision"] = (mod >> 16) & 0xFFFF
        out["scale"] = ((mod & 0x7FF) ^ 1024) - 1024    # 11-bit signed (negative scale allowed since PG 15)
    elif base in ("time", "timetz", "timestamp", "timestamptz") and typmod >= 0:
        out["precision"] = typmod
    elif base == "interval" and typmod >= 0:
        precision = typmod & 0xFFFF
        if precision != 0xFFFF:
            out["precision"] = precision
    if base in ("time", "timestamp"):
        out["with_time_zone"] = False
    elif base in ("timetz", "timestamptz"):
        out["with_time_zone"] = True
    if base in ("json", "jsonb"):
        out["binary_json"] = base == "jsonb"
    return out
