"""The attempt log of one case: every action and its exact result.

Entries are kept in memory (for the LLM and the next attempt to read) and
appended to runs/<case>/<UTC time>.jsonl as they happen, one JSON object per
line, flushed immediately. A crash loses at most the line being written, and
the reader skips a torn last line.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import re
import time
import traceback
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Protocol

import psycopg2

from ..errors import CexError
from .entry import STATUSES, STEPS, Entry


log = logging.getLogger(__name__)


class JournalListener(Protocol):
    """Told about every action as it happens (the live console, the run log file)."""

    def started(self, case: str, step: str, action: str) -> None: ...

    def recorded(self, entry: Entry, journal: "Journal") -> None: ...


# Listeners for every journal in this process. The CLI adds the console and the run log here.
LISTENERS: list[JournalListener] = []


def add_listener(listener: JournalListener) -> None:
    if listener not in LISTENERS:
        LISTENERS.append(listener)


def remove_listener(listener: JournalListener) -> None:
    if listener in LISTENERS:
        LISTENERS.remove(listener)


class Journal:
    def __init__(self, case: str, path: Path | None = None):
        """`path=None` keeps the log in memory only (tests, dry runs)."""
        self.case = case
        self.path = path
        self.entries: list[Entry] = []
        self._file = None
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._file = open(path, "a", encoding="utf-8")

    @classmethod
    def for_case(cls, case: str, runs_dir: str | Path) -> "Journal":
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
        return cls(case, Path(runs_dir) / case_dirname(case) / f"{stamp}.jsonl")

    # -- writing ---------------------------------------------------------------
    def record(self, step: str, action: str, status: str = "ok", *, detail: dict | None = None,
               error: dict | BaseException | None = None, llm: dict | None = None,
               duration_ms: float | None = None, at: str | None = None) -> Entry:
        if step not in STEPS:
            raise ValueError(f"unknown journal step {step!r}; add it to journal.entry.STEPS")
        if status not in STATUSES:
            raise ValueError(f"unknown journal status {status!r}")
        entry = Entry(
            seq=len(self.entries) + 1, case=self.case, step=step, action=action, status=status,
            at=at or _now(), duration_ms=None if duration_ms is None else round(duration_ms, 3),
            detail=detail or {}, error=describe_error(error) if isinstance(error, BaseException) else error, llm=llm,
        )
        self.entries.append(entry)
        if self._file is not None:
            self._file.write(json.dumps(entry.to_json(), ensure_ascii=False) + "\n")
            self._file.flush()
        for listener in list(LISTENERS):
            try:
                listener.recorded(entry, self)
            except Exception:                     # a broken listener must never break a run
                log.exception("journal listener failed")
        return entry

    def begin(self, step: str, action: str) -> None:
        """Announce that an action starts (shown live; the entry itself is recorded when it ends)."""
        for listener in list(LISTENERS):
            try:
                listener.started(self.case, step, action)
            except Exception:
                log.exception("journal listener failed")

    @contextmanager
    def timed(self, step: str, action: str, **kw) -> Iterator[dict]:
        """Record an action with its duration. The block may add to the yielded `detail`
        dict. An exception is recorded as a failure and then re-raised."""
        detail: dict[str, Any] = dict(kw.pop("detail", None) or {})
        status = kw.pop("status", "ok")
        self.begin(step, action)
        at, t0 = _now(), time.perf_counter()
        try:
            yield detail
        except BaseException as e:
            self.record(step, action, "failed", detail=detail, error=e,
                        duration_ms=(time.perf_counter() - t0) * 1000, at=at, **kw)
            raise
        self.record(step, action, status, detail=detail,
                    duration_ms=(time.perf_counter() - t0) * 1000, at=at, **kw)

    def close(self) -> None:
        if self._file is not None and not self._file.closed:
            self._file.close()

    def __enter__(self) -> "Journal":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- reading ---------------------------------------------------------------
    def by_step(self, step: str) -> list[Entry]:
        return [e for e in self.entries if e.step == step]

    def failures(self, step: str | None = None) -> list[Entry]:
        return [e for e in self.entries if e.status == "failed" and (step is None or e.step == step)]

    def last(self, step: str) -> Entry | None:
        found = self.by_step(step)
        return found[-1] if found else None

    @staticmethod
    def load(path: str | Path) -> list[Entry]:
        """Read a journal file back. A torn last line (crash mid-write) is skipped."""
        entries = []
        lines = Path(path).read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                entries.append(Entry.from_json(json.loads(line)))
            except (json.JSONDecodeError, TypeError):
                if i == len(lines) - 1:
                    break
                raise
        return entries


def describe_error(e: BaseException) -> dict[str, Any]:
    out: dict[str, Any] = {"type": type(e).__name__, "message": str(e).strip()}
    if isinstance(e, psycopg2.Error):
        out["sqlstate"] = e.pgcode
        if e.diag is not None:
            for key, attr in (("constraint", "constraint_name"), ("table", "table_name"), ("column", "column_name")):
                value = getattr(e.diag, attr, None)
                if value:
                    out[key] = value
    elif not isinstance(e, CexError):          # an unexpected bug: keep where it happened
        out["traceback"] = "".join(traceback.format_exception(type(e), e, e.__traceback__)[-3:])
    for attr in ("sqlstate", "line", "column", "hint"):    # SchemaError fields
        value = getattr(e, attr, None)
        if value is not None and attr not in out:
            out[attr] = value
    return out


def case_dirname(case: str) -> str:
    """A safe folder name for a case. A short hash keeps 'a:b' and 'a_b' apart."""
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", case).strip("._") or "case"
    digest = hashlib.sha256(case.encode()).hexdigest()[:8]
    return f"{safe[:80]}-{digest}"


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

