"""Opening connections: timeouts, fixed session settings, clear failures."""
from __future__ import annotations

import psycopg2
import psycopg2.extensions

from ..config import Settings
from ..errors import DatabaseUnavailable


def connect(settings: Settings, dbname: str | None = None, autocommit: bool = False):
    """Connect to `dbname` (default: the DSN's database) with the session pinned
    to `settings.session`, so every connection behaves the same everywhere."""
    options = " ".join(f"-c {k}={_escape_option(v)}" for k, v in settings.session.items())
    kwargs = {
        "connect_timeout": settings.connect_timeout_s,
        "application_name": "cexgen",
        "client_encoding": "UTF8",
        "options": options,
    }
    if dbname:
        kwargs["dbname"] = dbname
    try:
        conn = psycopg2.connect(settings.dsn, **kwargs)
    except psycopg2.OperationalError as e:
        raise DatabaseUnavailable(_explain(settings.dsn, dbname, e)) from None
    except psycopg2.ProgrammingError as e:          # malformed DSN
        raise DatabaseUnavailable(f"invalid connection string {safe_dsn(settings.dsn)!r}: {e}") from None
    conn.autocommit = autocommit
    return conn


def safe_dsn(dsn: str) -> str:
    """The DSN with any password removed, for messages and logs."""
    try:
        parts = psycopg2.extensions.parse_dsn(dsn)
    except psycopg2.ProgrammingError:
        return "<unparseable dsn>"
    parts.pop("password", None)
    return psycopg2.extensions.make_dsn(**parts)


def _explain(dsn: str, dbname: str | None, e: psycopg2.OperationalError) -> str:
    text = str(e).strip()
    low = text.lower()
    where = safe_dsn(dsn) + (f" (database {dbname})" if dbname else "")
    if "password authentication failed" in low or "authentication failed" in low:
        return f"Postgres refused the login for {where}: check user/password in CEX_DSN"
    if "does not exist" in low:
        return f"{text} — check CEX_DSN ({where})"
    if any(s in low for s in ("connection refused", "could not connect", "timeout expired", "no such file",
                              "could not translate host name", "server closed the connection")):
        return (f"cannot reach Postgres at {where}.\n"
                f"  Start the bundled one with:  docker compose up -d\n"
                f"  or point CEX_DSN at another server.\n  ({text.splitlines()[0]})")
    return f"cannot connect to Postgres at {where}: {text.splitlines()[0]}"


def _escape_option(value: str) -> str:
    # libpq "options" are space-separated; spaces and backslashes in a value must be escaped.
    return value.replace("\\", "\\\\").replace(" ", "\\ ")
