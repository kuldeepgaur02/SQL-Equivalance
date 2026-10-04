"""The live console, the run log file, and the CLI flags around them."""
import io
import logging
from pathlib import Path

import pytest

from cexgen import cli
from cexgen.config import Settings
from cexgen.journal import Journal, add_listener, remove_listener
from cexgen.observe import LiveConsole, RunLog, Session


class Collect:
    def __init__(self):
        self.starts, self.entries = [], []

    def started(self, case, step, action):
        self.starts.append((case, step, action))

    def recorded(self, entry, journal):
        self.entries.append(entry)


def test_listeners_hear_starts_and_entries():
    c = Collect()
    add_listener(c)
    try:
        j = Journal("case-a")
        with j.timed("insert", "INSERT public.t"):
            pass
        j.record("repair", "fix", "failed", error={"message": "boom"})
    finally:
        remove_listener(c)
    assert c.starts == [("case-a", "insert", "INSERT public.t")]
    assert [(e.step, e.status) for e in c.entries] == [("insert", "ok"), ("repair", "failed")]


def test_a_broken_listener_never_breaks_a_run():
    class Broken:
        def started(self, *a):
            raise RuntimeError("x")

        def recorded(self, *a):
            raise RuntimeError("x")
    add_listener(Broken())
    try:
        Journal("c").record("insert", "x")                       # no exception
    finally:
        from cexgen.journal import LISTENERS
        LISTENERS.clear()


def test_console_levels():
    out = io.StringIO()
    quiet = LiveConsole(logging.WARNING, out)
    quiet.recorded(Journal("c").record("insert", "INSERT t", "failed"), None)
    assert out.getvalue() == ""                                  # quiet: the command prints its summary
    verbose = LiveConsole(logging.INFO, out)
    verbose.started("c", "run", "Step 6 · INSERT")
    verbose.started("c", "insert", "INSERT t")                   # internal start: only in --debug
    verbose.recorded(Journal("c").record("insert", "INSERT t", "failed",
                                         error={"sqlstate": "23514", "constraint": "t_x_check", "message": "no"}), None)
    text = out.getvalue()
    assert "▶ Step 6 · INSERT" in text and "… INSERT t" not in text
    assert "FAILED" in text and "23514 t_x_check no" in text
    assert "\033[" not in text                                   # not a terminal: no colours


def test_run_log_has_header_events_and_summary(tmp_path):
    s = Settings(runs_dir=str(tmp_path), dsn="postgresql://u:secret@h:1/d")
    log = RunLog(tmp_path / "x.log", "check", ["check", "examples"], s)
    j = Journal("case-a")
    log.started("case-a", "run", "Step 7 · compare")
    log.recorded(j.record("compare", "run Q1 and Q2", detail={"outcome": "differ", "kind": "rows"}), j)
    log.recorded(j.record("insert", "INSERT t", "failed", error={"message": "too long"}), j)
    log.recorded(j.record("handoff", "hand off", detail={"status": "counterexample", "file": "/h.json"}), j)
    log.recorded(j.record("run", "case finished", "ok"), j)
    log.emit(logging.LogRecord("cexgen.db", logging.DEBUG, "", 0, "sandbox %s dropped", ("db1",), None))
    log.finish(0)
    text = (tmp_path / "x.log").read_text()
    assert "command:   cexgen check examples" in text and "secret" not in text
    assert "▶ Step 7 · compare" in text and "sandbox db1 dropped" in text
    assert '"outcome": "differ"' in text                          # full detail is kept
    assert "cases:      1 run, 1 ok, 0 failed" in text and "{'differ (rows)': 1}" in text
    assert "{'counterexample': 1}" in text and "case-a: counterexample -> /h.json" in text
    assert "errors (1):" in text and "case-a | insert | INSERT t | too long" in text


def test_secrets_never_logged_by_http_libraries(tmp_path):
    session = Session("check", [], Settings(runs_dir=str(tmp_path)), verbosity=2, run_log=True)
    try:
        for name in ("anthropic", "httpx", "httpcore"):
            assert logging.getLogger(name).getEffectiveLevel() >= logging.WARNING
    finally:
        session.finish(0)


@pytest.mark.db
def test_cli_writes_a_run_log_and_logs_command_shows_it(tmp_path, capsys):
    code = cli.main(["check", "examples/shop", "--no-llm", "--runs-dir", str(tmp_path), "-v"])
    err = capsys.readouterr().err
    assert code == 0 and "▶ Step 9" in err and "run log:" in err
    [log] = list((tmp_path / "_logs").glob("*-check.log"))
    assert "SUMMARY" in log.read_text()
    cli.main(["logs", "--runs-dir", str(tmp_path)])
    assert "SUMMARY" in capsys.readouterr().out
    assert cli.main(["--no-run-log", "check", "examples/shop", "--no-llm", "--runs-dir", str(tmp_path)]) == 0
    assert len(list((tmp_path / "_logs").glob("*.log"))) == 1   # --no-run-log (before the command) honoured
    cli.main(["logs", "--clean", "0", "--runs-dir", str(tmp_path)])
    assert list((tmp_path / "_logs").glob("*.log")) == []


def test_crash_is_kept_with_its_traceback(tmp_path, monkeypatch, capsys):
    def boom(args):
        raise ZeroDivisionError("bug")
    monkeypatch.setattr(cli, "_cleanup", boom)
    monkeypatch.setattr(cli, "get_settings", lambda: Settings(runs_dir=str(tmp_path)))
    code = cli.main(["cleanup"])
    err = capsys.readouterr().err
    assert code == 1 and "cexgen crashed: ZeroDivisionError" in err
    path = Path(err.split("run log: ")[1].strip())
    text = path.read_text()
    assert "Traceback" in text and "ZeroDivisionError: bug" in text and "exit code: 1" in text
    assert path.parent == tmp_path / "_logs"
