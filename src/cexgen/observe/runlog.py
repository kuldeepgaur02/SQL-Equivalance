"""The run log: one file per CLI run, with everything that happened.

    runs/_logs/<UTC time>-<command>.log

    header    the command line, the settings (password hidden), the versions
    events    every step as it starts, every action with its full detail and errors,
              every log message from the internals (sandbox, SQL, LLM, memory), tracebacks
    summary   cases ok / failed, outcomes, hand-off statuses, LLM calls, time, and every error again

Written as it happens and flushed, so even an interrupted run leaves its log.
"""
from __future__ import annotations

import datetime as dt
import logging
import platform
import sys
import time
import traceback
from collections import Counter
from pathlib import Path

from .. import __version__
from ..config import Settings
from ..db.connection import safe_dsn
from ..journal.entry import Entry
from .format import CURRENT_CASE, clock, entry_body, entry_headline, headline


class RunLog(logging.Handler):
    def __init__(self, path: Path, command: str, argv: list[str], settings: Settings):
        super().__init__(logging.DEBUG)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.file = open(path, "a", encoding="utf-8")
        self.t0 = time.perf_counter()
        self.cases: dict[str, str] = {}               # case -> "ok" / "failed"
        self.case_outcome: dict[str, str] = {}      # case -> latest comparison outcome
        self.statuses: Counter = Counter()
        self.errors: list[str] = []
        self.llm_calls = 0
        self.tokens = Counter()
        self.handoffs: list[str] = []
        self._header(command, argv, settings)

    def write(self, text: str) -> None:
        if not self.file.closed:
            self.file.write(text + "\n")
            self.file.flush()

    def _header(self, command: str, argv: list[str], settings: Settings) -> None:
        lines = [
            "=" * 100,
            f"cexgen {__version__} run log — {dt.datetime.now().isoformat(timespec='seconds')} (local time)",
            f"command:   cexgen {' '.join(argv)}",
            f"python:    {platform.python_version()} on {platform.platform()}",
            f"database:  {safe_dsn(settings.dsn)}",
            f"llm:       {'on (' + settings.llm_provider + ' / ' + settings.llm_model + ')' if settings.llm_enabled else 'off (--no-llm: rule table)'}",
            f"runs dir:  {settings.runs_dir}   schema memory: {'on' if settings.use_schema_memory else 'off'}",
            f"limits:    max repairs 3, max rebuilds {settings.max_rebuilds}, max result rows {settings.max_result_rows}",
            "=" * 100,
        ]
        self.write("\n".join(lines))

    # -- journal listener ---------------------------------------------------------------
    def started(self, case: str, step: str, action: str) -> None:
        if action.startswith("Step "):
            self.write("")
            self.write(headline(case, "", f"▶ {action}"))
        else:
            self.write(headline(case, step, f"… {action}"))

    def recorded(self, entry: Entry, journal) -> None:
        self.write(entry_headline(entry))
        body = entry_body(entry)
        if body:
            self.write(body)
        if entry.status == "failed":
            msg = (entry.error or {}).get("message", "") if entry.error else ""
            self.errors.append(f"{entry.case} | {entry.step} | {entry.action} | {msg.splitlines()[0] if msg else ''}")
        if entry.llm:
            self.llm_calls += 1
            for k in ("input_tokens", "output_tokens"):
                if isinstance(entry.llm.get(k), int):
                    self.tokens[k] += entry.llm[k]
        if entry.step == "run" and entry.action in ("case finished", "case stopped", "case crashed"):
            self.cases[entry.case] = "ok" if entry.status == "ok" else "failed"
            if journal.path is not None:
                self.write(f"      attempt log: {journal.path}")
        outcome = entry.detail.get("outcome") if entry.step == "compare" else \
            (entry.detail.get("comparison") or {}).get("outcome") if entry.step == "rebuild" else None
        if outcome:                                    # the latest comparison of a case is its outcome
            kind = (entry.detail if entry.step == "compare" else entry.detail["comparison"]).get("kind")
            self.case_outcome[entry.case] = outcome + (f" ({kind})" if kind else "")
        if entry.step == "handoff" and entry.detail.get("status"):
            self.statuses[entry.detail["status"]] += 1
            if entry.detail.get("file"):
                self.handoffs.append(f"{entry.case}: {entry.detail['status']} -> {entry.detail['file']}")

    # -- python logging -------------------------------------------------------------------
    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = headline(CURRENT_CASE.get(), record.name.replace("cexgen.", ""),
                            f"{record.levelname}: {record.getMessage()}")
            if record.exc_info:
                text += "\n" + "".join(traceback.format_exception(*record.exc_info))
            self.write(text)
        except Exception:
            self.handleError(record)

    # -- the end ----------------------------------------------------------------------------
    def finish(self, exit_code: int, crash: BaseException | None = None) -> None:
        if crash is not None:
            self.write(f"{clock()}  CRASH: {type(crash).__name__}: {crash}")
            self.write("".join(traceback.format_exception(type(crash), crash, crash.__traceback__)))
        took = time.perf_counter() - self.t0
        ok = sum(v == "ok" for v in self.cases.values())
        lines = ["", "=" * 100, "SUMMARY",
                 f"  cases:      {len(self.cases)} run, {ok} ok, {len(self.cases) - ok} failed",
                 f"  outcomes:   {dict(Counter(self.case_outcome.values())) or '-'}",
                 f"  hand-offs:  {dict(self.statuses) or '-'}",
                 f"  llm:        {self.llm_calls} call(s), {self.tokens['input_tokens']} input / "
                 f"{self.tokens['output_tokens']} output tokens",
                 f"  time:       {took:.1f} s      exit code: {exit_code}"]
        if self.handoffs:
            lines.append("  hand-off files:")
            lines += [f"    {h}" for h in self.handoffs]
        if self.errors:
            lines.append(f"  errors ({len(self.errors)}):")
            lines += [f"    {e}" for e in self.errors]
        lines.append("=" * 100)
        self.write("\n".join(lines))
        self.file.close()
