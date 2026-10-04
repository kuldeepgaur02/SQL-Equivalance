"""Switching observation on for one CLI run.

    console   -v: the flow (steps, actions, results, warnings); --debug: also every internal detail
    run log   always (unless --no-run-log): runs/_logs/<time>-<command>.log, everything, DEBUG level
"""
from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path

from ..config import Settings
from ..journal import add_listener, remove_listener
from .console import LiveConsole
from .runlog import RunLog

# Third-party loggers that may print request details (headers, keys) at DEBUG: never below WARNING.
QUIET = ("anthropic", "httpx", "httpcore", "urllib3")


class Session:
    def __init__(self, command: str, argv: list[str], settings: Settings, verbosity: int, run_log: bool):
        level = logging.DEBUG if verbosity >= 2 else logging.INFO if verbosity == 1 else logging.WARNING
        root = logging.getLogger()
        root.setLevel(logging.DEBUG)
        for name in QUIET:
            logging.getLogger(name).setLevel(logging.WARNING)
        self.console = LiveConsole(level)
        root.addHandler(self.console)
        add_listener(self.console)
        self.runlog = None
        if run_log and settings.run_log:
            stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            path = Path(settings.runs_dir) / "_logs" / f"{stamp}-{command}.log"
            self.runlog = RunLog(path, command, argv, settings)
            root.addHandler(self.runlog)
            add_listener(self.runlog)

    @property
    def path(self) -> Path | None:
        return self.runlog.path if self.runlog else None

    def finish(self, exit_code: int, crash: BaseException | None = None) -> None:
        root = logging.getLogger()
        for part in (self.console, self.runlog):
            if part is None:
                continue
            remove_listener(part)
            root.removeHandler(part)
        if self.runlog is not None:
            self.runlog.finish(exit_code, crash)
