"""Where cases come from. Each source turns a path into a list of Cases.

  FolderSource  a folder with schema.sql, q1.sql, q2.sql (and optional meta.json)
  BatchSource   a .json file: one schema, many named query pairs
  VeriEQLSource a VeriEQL benchmark .jsonlines file (see veriEQL.py)
  SuiteSource   a folder of the above (searched recursively)

To support a new input format, write a class with `matches(path)` and
`load(path, settings)`, and add it to SOURCES ahead of SuiteSource.
"""
from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any, Protocol

from ..config import Settings, get_settings
from ..errors import InputError
from .case import Case
from .files import read_text
from .veriEQL import VeriEQLSource


class Source(Protocol):
    name: str

    def matches(self, path: Path) -> bool: ...

    def load(self, path: Path, settings: Settings) -> list[Case]: ...


class FolderSource:
    name = "folder"
    files = ("schema.sql", "q1.sql", "q2.sql")

    def matches(self, path: Path) -> bool:
        return path.is_dir() and any((path / f).exists() for f in self.files)

    def load(self, path: Path, settings: Settings) -> list[Case]:
        missing = [f for f in self.files if not (path / f).is_file()]
        if missing:
            raise InputError(f"{path}: case folder is missing {', '.join(missing)}")
        meta = {}
        if (path / "meta.json").exists():
            meta = _json_object(path / "meta.json", settings, "meta.json")
        read = lambda f: read_text(path / f, settings.max_input_bytes)
        return [Case(path.name, read("schema.sql"), read("q1.sql"), read("q2.sql"), source=str(path), meta=meta)]


class BatchSource:
    name = "batch"
    top_keys = {"name", "schema", "schema_file", "pairs", "meta"}
    pair_keys = {"name", "q1", "q2", "q1_file", "q2_file", "meta"}

    def matches(self, path: Path) -> bool:
        return path.is_file() and path.suffix.lower() == ".json"

    def load(self, path: Path, settings: Settings) -> list[Case]:
        spec = _json_object(path, settings, "batch file")
        _no_unknown_keys(spec, self.top_keys, f"{path}")

        has_inline, has_file = "schema" in spec, "schema_file" in spec
        if has_inline == has_file:
            raise InputError(f"{path}: give exactly one of 'schema' (inline SQL) or 'schema_file'")
        schema_sql = (_text_field(spec, "schema", str(path)) if has_inline
                      else read_text(_relative(path, _text_field(spec, "schema_file", str(path))), settings.max_input_bytes))

        pairs = spec.get("pairs")
        if not isinstance(pairs, list) or not pairs:
            raise InputError(f"{path}: 'pairs' must be a non-empty list")
        batch_meta = spec.get("meta", {})
        if not isinstance(batch_meta, dict):
            raise InputError(f"{path}: 'meta' must be an object")

        prefix = spec.get("name", path.stem)
        cases, seen = [], {}
        for i, pair in enumerate(pairs, 1):
            where = f"{path} pair {i}"
            if not isinstance(pair, dict):
                raise InputError(f"{where}: must be an object")
            _no_unknown_keys(pair, self.pair_keys, where)
            name = pair.get("name", f"{prefix}#{i}")
            if not isinstance(name, str):
                raise InputError(f"{where}: 'name' must be text")
            if name in seen:
                raise InputError(f"{where}: name {name!r} already used by pair {seen[name]}")
            seen[name] = i
            meta = {**batch_meta, **_dict_field(pair, "meta", where)}
            q1 = self._query(path, pair, "q1", where, settings)
            q2 = self._query(path, pair, "q2", where, settings)
            cases.append(Case(name, schema_sql, q1, q2, source=where, meta=meta))
        return cases

    @staticmethod
    def _query(path: Path, pair: dict, key: str, where: str, settings: Settings) -> str:
        inline, file = key in pair, f"{key}_file" in pair
        if inline == file:
            raise InputError(f"{where}: give exactly one of '{key}' or '{key}_file'")
        if inline:
            return _text_field(pair, key, where)
        return read_text(_relative(path, _text_field(pair, f"{key}_file", where)), settings.max_input_bytes)


class SuiteSource:
    """A folder holding case folders and/or batch files, at any depth."""

    name = "suite"

    def matches(self, path: Path) -> bool:
        return path.is_dir()

    def load(self, path: Path, settings: Settings) -> list[Case]:
        cases: list[Case] = []
        owner: dict[str, str] = {}
        for child in sorted(path.iterdir()):
            if child.name.startswith((".", "_")):
                continue
            source = next((s for s in SOURCES if s.matches(child)), None)
            if source is None or (isinstance(source, SuiteSource) and not _contains_cases(child)):
                continue
            for case in source.load(child, settings):
                if case.name in owner:
                    raise InputError(f"duplicate case name {case.name!r}: {owner[case.name]} and {case.source}")
                owner[case.name] = case.source
                cases.append(case)
        return cases


# Order matters: the first source whose `matches` is true loads the path.
SOURCES: list[Source] = [FolderSource(), BatchSource(), VeriEQLSource(), SuiteSource()]


def load_cases(path: str | Path, settings: Settings | None = None, sample: int | None = None,
               seed: int = 0) -> list[Case]:
    """Load every case under `path` (a case folder, a batch .json, a VeriEQL .jsonlines, or a suite folder).
    `sample`: keep a fixed random subset of that size (same seed -> same cases, in their original order)."""
    cases = _load_all(path, settings or get_settings())
    if sample is not None and 0 < sample < len(cases):
        keep = sorted(random.Random(seed).sample(range(len(cases)), sample))
        cases = [cases[i] for i in keep]
    return cases


def _load_all(path: str | Path, settings: Settings) -> list[Case]:
    p = Path(path).expanduser()
    if not p.exists():
        raise InputError(f"no such file or folder: {p}")
    for source in SOURCES:
        if source.matches(p):
            cases = source.load(p, settings)
            if not cases:
                raise InputError(f"{p}: no cases found (expected schema.sql/q1.sql/q2.sql folders or batch .json files)")
            return cases
    raise InputError(f"{p}: not a case folder, a batch .json file, or a folder of cases")


# -- helpers -------------------------------------------------------------------
def _json_object(path: Path, settings: Settings, what: str) -> dict:
    text = read_text(path, settings.max_input_bytes)
    try:
        value = json.loads(text)
    except json.JSONDecodeError as e:
        raise InputError(f"{path}: invalid JSON in {what} at line {e.lineno}, column {e.colno}: {e.msg}") from None
    if not isinstance(value, dict):
        raise InputError(f"{path}: {what} must be a JSON object")
    return value


def _no_unknown_keys(obj: dict, allowed: set[str], where: str) -> None:
    unknown = sorted(set(obj) - allowed)
    if unknown:   # a typo like "querry1" should fail loudly, not be ignored
        raise InputError(f"{where}: unknown key(s) {', '.join(map(repr, unknown))}; allowed: {', '.join(sorted(allowed))}")


def _text_field(obj: dict, key: str, where: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str):
        raise InputError(f"{where}: '{key}' must be text")
    return value


def _dict_field(obj: dict, key: str, where: str) -> dict[str, Any]:
    value = obj.get(key, {})
    if not isinstance(value, dict):
        raise InputError(f"{where}: '{key}' must be an object")
    return value


def _relative(base_file: Path, ref: str) -> Path:
    p = Path(ref).expanduser()
    return p if p.is_absolute() else base_file.parent / p


def _contains_cases(folder: Path) -> bool:
    return any(not c.name.startswith((".", "_")) and (FolderSource().matches(c) or BatchSource().matches(c)
                                                     or (c.is_dir() and _contains_cases(c)))
               for c in folder.iterdir())
