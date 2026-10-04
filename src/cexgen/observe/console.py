"""The live view: what is happening, printed as it happens.

    -v / --verbose   the flow: each step as it starts, every action with its result, warnings
    --debug          also the internals: sandbox databases, schema SQL, every SQL statement,
                     LLM requests, memory; and the full detail of every failure
Colours: green ok, red failed / errors, yellow skipped / warnings, cyan the flow (off if not a terminal or NO_COLOR is set).
"""
from __future__ import annotations

import logging
import os
import sys
import traceback

from ..journal.entry import Entry
from .format import CURRENT_CASE, entry_body, entry_headline, headline

COLOURS = {"ok": "32", "failed": "31", "skipped": "33", "info": "36", "flow": "1;36", "warning": "33",
           "error": "1;31", "debug": "2"}


class LiveConsole(logging.Handler):
    def __init__(self, level: int, stream=None):
        super().__init__(level)
        self.stream = stream or sys.stderr
        self.colour = hasattr(self.stream, "isatty") and self.stream.isatty() and "NO_COLOR" not in os.environ

    def paint(self, text: str, kind: str) -> str:
        return f"\033[{COLOURS[kind]}m{text}\033[0m" if self.colour and kind in COLOURS else text

    def write(self, text: str) -> None:
        self.stream.write(text + "\n")
        self.stream.flush()

    # -- journal listener -------------------------------------------------------------
    def started(self, case: str, step: str, action: str) -> None:
        if self.level > logging.INFO:
            return
        if action.startswith("Step "):
            self.write(self.paint(headline(case, "", f"▶ {action}"), "flow"))
        elif self.level <= logging.DEBUG:
            self.write(self.paint(headline(case, step, f"… {action}"), "debug"))

    def recorded(self, entry: Entry, journal) -> None:
        if self.level > logging.INFO:                      # quiet mode: the command prints its own summary
            return
        self.write(self.paint(entry_headline(entry), entry.status))
        if entry.status == "failed" and self.level <= logging.DEBUG:
            body = entry_body(entry)
            if body:
                self.write(self.paint(body, "debug"))

    # -- python logging ---------------------------------------------------------------------
    def emit(self, record: logging.LogRecord) -> None:
        try:
            kind = "error" if record.levelno >= logging.ERROR else "warning" if record.levelno >= logging.WARNING \
                else "debug" if record.levelno < logging.INFO else "info"
            text = headline(CURRENT_CASE.get(), record.name.replace("cexgen.", ""),
                            f"{record.levelname.lower()}: {record.getMessage()}")
            if record.exc_info:
                text += "\n" + "".join(traceback.format_exception(*record.exc_info))
            self.write(self.paint(text, kind))
        except Exception:
            self.handleError(record)
