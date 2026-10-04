"""Step 7 — Run Q1 and Q2, compare.

  both fail with the same error            -> same
  one fails, or they fail differently      -> differ (error)
  different rows, as a bag                 -> differ (rows)    order ignored, duplicates count, NULL = NULL only
  the same values in another column order  -> differ (columns)  reported apart: often SELECT * on a schema
                                                               whose column order differs from the source
  same rows, both empty                    -> both empty
  same rows, both have ORDER BY, and the   -> differ (order)   only where ORDER BY decides it:
    orders cannot be the same                                  rows tied on the sort key are interchangeable
  otherwise                                -> same
"""
from __future__ import annotations

from collections import Counter
from itertools import islice

from .model import BOTH_EMPTY, COLUMNS, DIFFER, ERROR, ORDER, ROWS, SAME, Comparison, QueryRun
from .normalize import row_key, value_key

SAMPLE = 20
MAX_ORDER_CHECK = 5000          # distinct rows; beyond that the order is not compared (noted)


def compare(r1: QueryRun, r2: QueryRun, not_deterministic: list[str] | tuple = (),
            reference: dict | None = None) -> Comparison:
    def result(outcome, kind, reason, only1=(), only2=(), extra=()):
        return Comparison(outcome, kind, reason, r1, r2, tuple(only1), tuple(only2),
                          tuple(notes) + tuple(extra), tuple(not_deterministic), reference)

    notes: list[str] = list(r1.notes) + list(r2.notes)
    for r in (r1, r2):
        if r.truncated:
            notes.append(f"{r.label} returned more rows than the comparison reads; only the first ones were compared")

    if r1.error or r2.error:
        if r1.error and r2.error and r1.error.get("sqlstate") == r2.error.get("sqlstate"):
            return result(SAME, None, f"both fail with the same error ({r1.error['sqlstate']})")
        if r1.error and r2.error:
            return result(DIFFER, ERROR, f"they fail with different errors: Q1 {r1.error['sqlstate']} "
                                         f"({r1.error['message']}), Q2 {r2.error['sqlstate']} ({r2.error['message']})")
        failing, other = (r1, r2) if r1.error else (r2, r1)
        return result(DIFFER, ERROR, f"{failing.label} fails ({failing.error['sqlstate']}: {failing.error['message']}) "
                                     f"while {other.label} returns {len(other.rows)} row(s)")

    if len(r1.columns) != len(r2.columns):
        if not r1.rows and not r2.rows:
            return result(BOTH_EMPTY, None, "both return no rows",
                          extra=[f"Q1 has {len(r1.columns)} column(s), Q2 has {len(r2.columns)}"])
        return result(DIFFER, ROWS, f"Q1 returns {len(r1.columns)} column(s), Q2 returns {len(r2.columns)}",
                      _sample(Counter(row_key(r, r1.type_oids) for r in r1.rows), r1),
                      _sample(Counter(row_key(r, r2.type_oids) for r in r2.rows), r2))

    for i, (a, b) in enumerate(zip(r1.types, r2.types), 1):
        if a != b:
            notes.append(f"column {i}: Q1 gives {a}, Q2 gives {b} (compared by value)")

    bag1 = Counter(row_key(r, r1.type_oids) for r in r1.rows)
    bag2 = Counter(row_key(r, r2.type_oids) for r in r2.rows)
    if bag1 != bag2:
        only1, only2 = bag1 - bag2, bag2 - bag1
        order = column_permutation(bag1, bag2, len(r1.columns))
        if order is not None:
            moved = ", ".join(f"Q1 column {i + 1} is Q2 column {j + 1}" for i, j in enumerate(order) if i != j)
            return result(DIFFER, COLUMNS, f"the same values in a different column order ({moved})",
                          _sample(only1, r1), _sample(only2, r2))
        reason = (f"Q1 returns {len(r1.rows)} row(s), Q2 returns {len(r2.rows)}" if len(r1.rows) != len(r2.rows)
                  else "same number of rows, different rows")
        return result(DIFFER, ROWS, reason, _sample(only1, r1), _sample(only2, r2))
    if not r1.rows:
        return result(BOTH_EMPTY, None, "both return no rows")

    if r1.ordered and r2.ordered:
        if r1.keys is None or r2.keys is None:
            notes.append("both have ORDER BY, but the order could not be compared (see notes)")
        else:
            conflict = order_conflict(r1, r2)
            if conflict == "too many":
                notes.append(f"more than {MAX_ORDER_CHECK} distinct rows: the order was not compared")
            elif conflict:
                return result(DIFFER, ORDER, conflict)
    return result(SAME, None, f"same {len(r1.rows)} row(s)")


def column_permutation(bag1: Counter, bag2: Counter, width: int) -> tuple[int, ...] | None:
    """A reordering of Q1's columns that makes both results equal, if one exists.
    Columns are matched by the bag of values they hold; then the whole rows are checked."""
    if width < 2 or sum(bag1.values()) != sum(bag2.values()):
        return None
    def column_bag(bag, i):
        return Counter(_expand(bag, i))
    sig1 = [column_bag(bag1, i) for i in range(width)]
    sig2 = [column_bag(bag2, j) for j in range(width)]
    order, used = [], set()
    for i in range(width):
        j = next((j for j in range(width) if j not in used and sig2[j] == sig1[i]), None)
        if j is None:
            return None
        order.append(j)
        used.add(j)
    if order == list(range(width)):
        return None
    permuted = Counter()
    for row, n in bag1.items():
        moved = [None] * width
        for i, j in enumerate(order):
            moved[j] = row[i]
        permuted[tuple(moved)] += n
    return tuple(order) if permuted == bag2 else None


def _expand(bag: Counter, column: int):
    for row, n in bag.items():
        for _ in range(n):
            yield row[column]


def order_conflict(r1: QueryRun, r2: QueryRun) -> str | None:
    """A pair of rows that Q1's ORDER BY puts strictly before the other while Q2's ORDER BY
    puts it strictly after. Rows tied on the sort key never conflict."""
    span1, span2 = _spans(r1), _spans(r2)
    distinct = list(span1)
    if len(distinct) > MAX_ORDER_CHECK:
        return "too many"
    for i, a in enumerate(distinct):
        for b in distinct[i + 1:]:
            a1, b1, a2, b2 = span1[a], span1[b], span2.get(a), span2.get(b)
            if a2 is None or b2 is None:
                continue
            if a1[1] < b1[0] and b2[1] < a2[0]:        # Q1: a strictly before b; Q2: b strictly before a
                first, second = a, b
            elif b1[1] < a1[0] and a2[1] < b2[0]:      # Q1: b strictly before a; Q2: a strictly before b
                first, second = b, a
            else:
                continue
            return (f"the same rows in a different order: Q1's ORDER BY puts {_show(first, r1)} before "
                    f"{_show(second, r1)}, Q2's puts them the other way round")
    return None


def _spans(r: QueryRun) -> dict[tuple, tuple[int, int]]:
    """row -> (first, last) tie group it appears in. A tie group is a run of equal sort keys."""
    spans: dict[tuple, tuple[int, int]] = {}
    group, previous = -1, object()
    for row, key in zip(r.rows, r.keys):
        k = tuple(value_key(v) for v in key)
        if k != previous:
            group, previous = group + 1, k
        rk = row_key(row, r.type_oids)
        lo, hi = spans.get(rk, (group, group))
        spans[rk] = (min(lo, group), max(hi, group))
    return spans


def _sample(counter: Counter, run: QueryRun) -> list[tuple[tuple, int]]:
    """(an original row, extra copies) for the most frequent differing rows."""
    originals = {row_key(r, run.type_oids): r for r in run.rows}
    return [(originals[k], n) for k, n in islice(counter.most_common(), SAMPLE)]


def _show(key: tuple, run: QueryRun) -> str:
    original = next((r for r in run.rows if row_key(r, run.type_oids) == key), key)
    return "(" + ", ".join(repr(v) for v in original) + ")"
