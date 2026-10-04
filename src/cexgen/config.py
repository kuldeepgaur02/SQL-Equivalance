"""Settings for every step, read once from the environment.

Nothing else in the code reads environment variables, so a test or another
tool can build its own `Settings(...)` and pass it in.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, replace
from functools import lru_cache

_PREFIX_RE = re.compile(r"^[a-z][a-z0-9_]{0,19}$")


@dataclass(frozen=True)
class Settings:
    # Connection to a "maintenance" database; each run creates its own sandbox database next to it.
    dsn: str = "postgresql://cex:cex@localhost:5433/cex"
    connect_timeout_s: int = 5

    # Sandbox databases are named <prefix>_<UTC timestamp>_<random>, so leftovers can be found and dropped.
    db_prefix: str = "cex"
    keep_db: bool = False

    # Fixed so query results do not depend on the machine running them.
    # C collation makes text ordering and comparison byte-wise and identical everywhere.
    db_encoding: str = "UTF8"
    db_locale: str = "C"
    session: dict[str, str] = field(default_factory=lambda: {
        "TimeZone": "UTC",
        "DateStyle": "ISO,YMD",
        "IntervalStyle": "postgres",
        "extra_float_digits": "3",       # floats print exactly, so equal values compare equal
        "statement_timeout": "5000",     # ms; a runaway query is an error, not a hang
        "lock_timeout": "2000",          # ms
        "idle_in_transaction_session_timeout": "60000",
    })

    # 13+: DROP DATABASE ... WITH (FORCE).
    min_server_version: int = 130000

    # Input files larger than this are rejected before reading them into memory.
    max_input_bytes: int = 10 * 1024 * 1024

    # Step 8: rounds of "rebuild the base -> INSERT -> compare" while both results are empty.
    max_rebuilds: int = 3

    # Step 9: re-create the handed-off data in a fresh database and compare again, to prove it reproduces.
    verify_handoff: bool = True

    # Step 7 reads at most this many rows of each query's result (a bigger result is flagged as truncated).
    max_result_rows: int = 100_000

    # Every CLI run writes <runs_dir>/_logs/<time>-<command>.log with everything that happened.
    run_log: bool = True

    # Attempt logs go to <runs_dir>/<case>/, schema memory to <runs_dir>/_schemas/.
    runs_dir: str = "runs"
    # Reuse (and update) what earlier runs learned about a schema. Off = a clean run (--fresh).
    use_schema_memory: bool = True

    # LLM. Off (--no-llm) = baseline mode: the same pipeline with the rule table instead.
    llm_enabled: bool = True
    llm_provider: str = "claude"            # a name registered in cexgen.llm.registry
    llm_model: str = "claude-opus-5-5"
    llm_effort: str = "medium"              # low | medium | high | xhigh | max
    llm_max_tokens: int = 16000
    llm_timeout_s: int = 120

    def __post_init__(self) -> None:
        if not _PREFIX_RE.match(self.db_prefix):
            raise ValueError(f"db_prefix {self.db_prefix!r} must match {_PREFIX_RE.pattern}")
        if self.connect_timeout_s < 1:
            raise ValueError("connect_timeout_s must be >= 1")
        if self.max_input_bytes < 1:
            raise ValueError("max_input_bytes must be >= 1")

    def with_(self, **changes) -> "Settings":
        return replace(self, **changes)

    @classmethod
    def from_env(cls) -> "Settings":
        base = cls()
        session = dict(base.session)
        if "CEX_STATEMENT_TIMEOUT_MS" in os.environ:
            session["statement_timeout"] = str(_int_env("CEX_STATEMENT_TIMEOUT_MS"))
        return cls(
            dsn=os.environ.get("CEX_DSN", base.dsn),
            connect_timeout_s=_int_env("CEX_CONNECT_TIMEOUT_S", base.connect_timeout_s),
            db_prefix=os.environ.get("CEX_DB_PREFIX", base.db_prefix),
            keep_db=os.environ.get("CEX_KEEP_DB", "").lower() in ("1", "true", "yes"),
            session=session,
            max_input_bytes=_int_env("CEX_MAX_INPUT_BYTES", base.max_input_bytes),
            runs_dir=os.environ.get("CEX_RUNS_DIR", base.runs_dir),
            llm_provider=os.environ.get("CEX_LLM_PROVIDER", base.llm_provider),
            llm_model=os.environ.get("CEX_LLM_MODEL", base.llm_model),
            llm_effort=os.environ.get("CEX_LLM_EFFORT", base.llm_effort),
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()


def _int_env(name: str, default: int | None = None) -> int:
    raw = os.environ.get(name)
    if raw is None:
        assert default is not None
        return default
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"environment variable {name} must be an integer, got {raw!r}") from None
