"""What has been learned about a schema, kept across runs.

Example: "a valid users row needs age >= 18; age = 18 worked". The next case
or tomorrow's run on the same schema starts from that, instead of hitting the
same CHECK error and paying for the same LLM repair again.

Stored at <runs_dir>/_schemas/<fingerprint>-pg<major>.json. The key is the
schema text's fingerprint plus the Postgres major version, so a changed schema
(or server) never reuses old knowledge. `use_schema_memory=False` (--fresh)
neither reads nor writes it, for clean evaluation runs.

Facts are grouped in namespaces, which later steps define:
  "base_rows"     table -> a row known to insert cleanly
  "failed_fixes"  table -> fixes that were tried and failed
Values must be JSON data.
"""
from __future__ import annotations

import copy
import datetime as dt
import json
import logging
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ..config import Settings, get_settings
from ..journal.entry import to_jsonable

log = logging.getLogger(__name__)

FORMAT_VERSION = 1

try:                                    # advisory file lock on POSIX; elsewhere writes are last-writer-wins
    import fcntl
except ImportError:                     # pragma: no cover
    fcntl = None


class SchemaMemory:
    def __init__(self, key: str, path: Path | None, facts: dict[str, dict[str, Any]] | None = None):
        self.key = key
        self.path = path                # None: in memory only
        self._facts: dict[str, dict[str, Any]] = facts or {}
        self._changed: dict[tuple[str, str], Any] = {}
        self._deleted: set[tuple[str, str]] = set()

    @classmethod
    def open(cls, fingerprint: str, server_version: int, settings: Settings | None = None) -> "SchemaMemory":
        settings = settings or get_settings()
        key = f"{fingerprint}-pg{server_version // 10000}"
        if not settings.use_schema_memory:
            return cls(key, None)
        path = Path(settings.runs_dir) / "_schemas" / f"{key}.json"
        return cls(key, path, _read(path, key))

    @property
    def persistent(self) -> bool:
        return self.path is not None

    # -- facts -----------------------------------------------------------------
    def get(self, namespace: str, key: str, default: Any = None) -> Any:
        value = self._facts.get(namespace, {}).get(key, default)
        return copy.deepcopy(value)

    def put(self, namespace: str, key: str, value: Any) -> None:
        value = to_jsonable(value)
        json.dumps(value)                                   # fail now, not at save time
        self._facts.setdefault(namespace, {})[key] = value
        self._changed[(namespace, key)] = value
        self._deleted.discard((namespace, key))

    def delete(self, namespace: str, key: str) -> None:
        self._facts.get(namespace, {}).pop(key, None)
        self._changed.pop((namespace, key), None)
        self._deleted.add((namespace, key))

    def namespace(self, namespace: str) -> dict[str, Any]:
        return copy.deepcopy(self._facts.get(namespace, {}))

    # -- persistence -------------------------------------------------------------
    def save(self) -> None:
        """Write our changes. Another process may have saved meanwhile, so the file is
        re-read under a lock and only the keys we changed are applied on top of it."""
        if self.path is None or not (self._changed or self._deleted):
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _locked(self.path.with_suffix(".lock")):
            facts = _read(self.path, self.key)
            for (ns, k), v in self._changed.items():
                facts.setdefault(ns, {})[k] = v
            for ns, k in self._deleted:
                facts.get(ns, {}).pop(k, None)
            _write_atomic(self.path, {
                "format": FORMAT_VERSION,
                "key": self.key,
                "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                "facts": {ns: v for ns, v in facts.items() if v},
            })
        self._facts = facts
        self._changed.clear()
        self._deleted.clear()


def _read(path: Path, key: str) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("format") != FORMAT_VERSION or data.get("key") != key or not isinstance(data.get("facts"), dict):
            raise ValueError("unexpected format")
        return data["facts"]
    except (OSError, ValueError, AttributeError) as e:
        # A damaged file must not stop a run: set it aside and start empty.
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S")
        aside = path.with_name(f"{path.name}.corrupt-{stamp}")
        try:
            path.rename(aside)
        except OSError:
            pass
        log.warning("schema memory %s is unreadable (%s); moved to %s and starting empty", path, e, aside.name)
        return {}


def _write_atomic(path: Path, data: dict) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)                  # readers see the old file or the new one, never half
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


@contextmanager
def _locked(lock_path: Path):
    with open(lock_path, "a") as f:
        if fcntl is not None:
            fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(f, fcntl.LOCK_UN)
