"""Reading input files safely: size limit, UTF-8 (with or without BOM), clear errors."""
from __future__ import annotations

from pathlib import Path

from ..errors import InputError


def read_text(path: Path, max_bytes: int) -> str:
    if not path.exists():
        raise InputError(f"missing file: {path}")
    if not path.is_file():
        raise InputError(f"not a regular file: {path}")
    size = path.stat().st_size
    if size > max_bytes:
        raise InputError(f"{path}: {size} bytes is over the {max_bytes}-byte limit (CEX_MAX_INPUT_BYTES)")
    try:
        data = path.read_bytes()
    except OSError as e:
        raise InputError(f"cannot read {path}: {e.strerror}") from None
    try:
        return data.decode("utf-8-sig")      # strips a UTF-8 BOM if an editor added one
    except UnicodeDecodeError as e:
        line = data.count(b"\n", 0, e.start) + 1
        raise InputError(f"{path}: not valid UTF-8 (byte {e.start}, line {line}); "
                         f"re-save the file as UTF-8") from None
