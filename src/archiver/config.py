"""Run configuration: where files come from, where they go, where work lives."""
from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

COMPRESSION_SUFFIX = {"none": ".tar", "gz": ".tar.gz", "xz": ".tar.xz"}
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _xdg(var: str, fallback: str) -> Path:
    value = os.environ.get(var)
    return Path(value) if value else Path.home() / fallback


def default_state_dir() -> Path:
    return _xdg("XDG_STATE_HOME", ".local/state") / "archiver"


def default_work_root() -> Path:
    return _xdg("XDG_CACHE_HOME", ".cache") / "archiver"


def default_run_id() -> str:
    return date.today().strftime("%Y-%m")


def validate_run_id(run_id: str) -> str:
    if not _RUN_ID_RE.match(run_id):
        raise ValueError(
            f"invalid run id {run_id!r}: use letters, digits, '.', '_' or '-' (e.g. 2026-10)"
        )
    return run_id


@dataclass(frozen=True)
class RunConfig:
    source_dir: Path
    dest: str                      # any rsync destination: /mnt/nas/x/ or nas:/volume1/x/
    run_id: str
    work_dir: Path                 # holds the file list, manifest and tarball
    state_dir: Path                # holds <run_id>.json and <run_id>.lock
    compression: str = "gz"
    rsync_cmd: tuple[str, ...] = ("rsync",)
    rsync_opts: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def build(
        cls,
        source_dir: str | Path,
        dest: str,
        *,
        run_id: str | None = None,
        work_dir: str | Path | None = None,
        state_dir: str | Path | None = None,
        compression: str = "gz",
        rsync: str | None = None,
        rsync_opts: str = "",
    ) -> "RunConfig":
        run_id = validate_run_id(run_id or default_run_id())
        if compression not in COMPRESSION_SUFFIX:
            raise ValueError(f"compression must be one of {sorted(COMPRESSION_SUFFIX)}")

        source = Path(source_dir).expanduser().resolve()
        if not source.is_dir():
            raise ValueError(f"source directory does not exist: {source}")

        work = Path(work_dir).expanduser().resolve() if work_dir else default_work_root() / run_id
        if work == source or source in work.parents:
            raise ValueError(
                f"work dir {work} is inside the source dir; the archive would include itself. "
                "Pass --work-dir somewhere else."
            )

        rsync_cmd = tuple(shlex.split(rsync or os.environ.get("ARCHIVER_RSYNC", "rsync")))
        if not rsync_cmd:
            raise ValueError("rsync command is empty")

        return cls(
            source_dir=source,
            dest=dest,
            run_id=run_id,
            work_dir=work,
            state_dir=Path(state_dir).expanduser().resolve() if state_dir else default_state_dir(),
            compression=compression,
            rsync_cmd=rsync_cmd,
            rsync_opts=tuple(shlex.split(rsync_opts)),
        )

    @property
    def filelist_path(self) -> Path:
        return self.work_dir / "filelist.json"

    @property
    def manifest_path(self) -> Path:
        return self.work_dir / f"{self.run_id}.md5"

    @property
    def tar_path(self) -> Path:
        return self.work_dir / f"{self.run_id}{COMPRESSION_SUFFIX[self.compression]}"
