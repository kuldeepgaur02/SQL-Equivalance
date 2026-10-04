"""Step 7's result: what each query returned, and how the two results compare."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

DIFFER = "differ"
SAME = "same"
BOTH_EMPTY = "both_empty"

ROWS = "rows"        # different rows (as a bag: order ignored, duplicates count)
ORDER = "order"      # same rows, but both queries ask for an order and the orders disagree
ERROR = "error"      # one query fails at run time and the other does not (or different errors)
COLUMNS = "columns"  # the same values, but in a different column order (e.g. SELECT * vs listed columns)


@dataclass(frozen=True)
class QueryRun:
    label: str
    columns: tuple[str, ...] = ()
    types: tuple[str, ...] = ()              # Postgres type names of the result columns
    type_oids: tuple[int, ...] = ()
    rows: tuple[tuple, ...] = ()             # in the order Postgres returned them
    keys: tuple[tuple, ...] | None = None    # sort-key values per row, when the order is compared
    ordered: bool = False                    # the query has a top-level ORDER BY
    error: dict[str, Any] | None = None      # {"sqlstate", "message"} if it failed
    truncated: bool = False                  # more rows than max_result_rows
    duration_ms: float = 0.0
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class Comparison:
    outcome: str                             # DIFFER | SAME | BOTH_EMPTY
    kind: str | None                         # ROWS | ORDER | ERROR (when DIFFER)
    reason: str
    q1: QueryRun
    q2: QueryRun
    only_in_q1: tuple[tuple[tuple, int], ...] = ()    # (row, how many more times in Q1), a sample
    only_in_q2: tuple[tuple[tuple, int], ...] = ()
    notes: tuple[str, ...] = ()              # e.g. column types differ, order not comparable
    not_deterministic: tuple[str, ...] = ()  # reasons a difference may not be real
    reference: dict[str, Any] | None = field(default=None)   # e.g. VeriEQL's verdict for this pair

    @property
    def counterexample(self) -> bool:
        """A difference that can be trusted: the data makes Q1 and Q2 behave differently."""
        return self.outcome == DIFFER and not self.not_deterministic
