"""Small helpers shared by the engine and both frontends."""
from __future__ import annotations

import contextlib
import os
import tempfile
from datetime import datetime
from pathlib import Path


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` so readers only ever see the old or new file.

    Writes to a temp file in the same directory, fsyncs it, then renames it
    over the destination. A crash mid-write leaves the old file intact.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def human_bytes(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or unit == "TiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    raise AssertionError("unreachable")


def describe_progress(unit: str, current: int, total: int) -> str:
    """Render a progress tuple for humans, e.g. '1.2 GiB / 4.0 GiB (30%)'."""
    if unit == "percent":
        return f"{current}%"
    if unit == "files":
        return f"{current:,} / {total:,} files" if total else f"{current:,} files"
    if unit == "bytes":
        if not total:
            return human_bytes(current)
        pct = int(current * 100 / total)
        return f"{human_bytes(current)} / {human_bytes(total)} ({pct}%)"
    return f"{current}/{total}" if total else str(current)
