"""Crash-safe record of how far a run has got.

One JSON file per run in the state dir (default ~/.local/state/archiver/):

    2026-10.json   stage statuses, progress, errors, timestamps
    2026-10.lock   flock'd while a process is running the pipeline

Every status change is written immediately; progress ticks are throttled.
Writes are atomic (temp file + rename), so a crash never leaves a torn file.
"""
from __future__ import annotations

import fcntl
import json
import os
import time
from dataclasses import asdict, dataclass, field, fields
from enum import Enum
from pathlib import Path
from typing import Iterable

from .util import atomic_write_bytes, now_iso

STATE_VERSION = 1


class StageStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class StageState:
    name: str
    status: StageStatus = StageStatus.PENDING
    current: int = 0
    total: int = 0
    detail: str = ""
    error: str | None = None
    started_at: str | None = None
    finished_at: str | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "StageState":
        known = {f.name for f in fields(cls)}
        data = {k: v for k, v in d.items() if k in known}
        data["status"] = StageStatus(data.get("status", "pending"))
        return cls(**data)


@dataclass
class RunState:
    run_id: str
    source_dir: str
    dest: str
    stages: dict[str, StageState] = field(default_factory=dict)
    created_at: str = field(default_factory=now_iso)

    @classmethod
    def new(cls, run_id: str, source_dir: str, dest: str, stage_names: Iterable[str]) -> "RunState":
        return cls(run_id, source_dir, dest, {n: StageState(n) for n in stage_names})

    def to_dict(self) -> dict:
        return {
            "version": STATE_VERSION,
            "run_id": self.run_id,
            "source_dir": self.source_dir,
            "dest": self.dest,
            "created_at": self.created_at,
            "stages": [s.to_dict() for s in self.stages.values()],
        }

    @classmethod
    def from_dict(cls, d: dict, stage_names: Iterable[str]) -> "RunState":
        saved = {s["name"]: StageState.from_dict(s) for s in d.get("stages", [])}
        # Keep the pipeline's stage order; tolerate stages added in newer versions.
        stages = {n: saved.get(n, StageState(n)) for n in stage_names}
        return cls(d["run_id"], d["source_dir"], d["dest"], stages, d.get("created_at", now_iso()))

    @property
    def complete(self) -> bool:
        return all(s.status is StageStatus.DONE for s in self.stages.values())

    def reset_from(self, name: str) -> None:
        """Mark ``name`` and every later stage as pending."""
        hit = False
        for key in self.stages:
            hit = hit or key == name
            if hit:
                self.stages[key] = StageState(key)
        if not hit:
            raise KeyError(name)


class StateStore:
    def __init__(self, state_dir: Path, min_save_interval: float = 0.5):
        self.state_dir = Path(state_dir)
        self.min_save_interval = min_save_interval
        self._last_save = 0.0

    def path_for(self, run_id: str) -> Path:
        return self.state_dir / f"{run_id}.json"

    def lock_path_for(self, run_id: str) -> Path:
        return self.state_dir / f"{run_id}.lock"

    def list_runs(self) -> list[str]:
        if not self.state_dir.is_dir():
            return []
        return sorted(p.stem for p in self.state_dir.glob("*.json"))

    def load(self, run_id: str, stage_names: Iterable[str]) -> RunState | None:
        path = self.path_for(run_id)
        if not path.exists():
            return None
        state = RunState.from_dict(json.loads(path.read_text()), stage_names)
        # A stage still marked RUNNING on load means the process that ran it died.
        for stage in state.stages.values():
            if stage.status is StageStatus.RUNNING and not self._is_locked(run_id):
                stage.status = StageStatus.FAILED
                stage.error = "interrupted: the process exited while this stage was running"
        return state

    def save(self, state: RunState) -> None:
        payload = json.dumps(state.to_dict(), indent=2).encode()
        atomic_write_bytes(self.path_for(state.run_id), payload)
        self._last_save = time.monotonic()

    def set_status(self, state: RunState, name: str, status: StageStatus, *, error: str | None = None) -> None:
        stage = state.stages[name]
        stage.status = status
        if status is StageStatus.RUNNING:
            stage.current = stage.total = 0
            stage.detail = ""
            stage.error = None
            stage.started_at = now_iso()
            stage.finished_at = None
        else:
            stage.finished_at = now_iso()
            stage.error = error
        self.save(state)

    def set_progress(self, state: RunState, name: str, current: int, total: int, detail: str) -> None:
        stage = state.stages[name]
        stage.current, stage.total, stage.detail = current, total, detail
        if time.monotonic() - self._last_save >= self.min_save_interval:
            self.save(state)

    def _is_locked(self, run_id: str) -> bool:
        path = self.lock_path_for(run_id)
        if not path.exists():
            return False
        with open(path, "a") as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(fh, fcntl.LOCK_UN)
            return False


class RunLocked(RuntimeError):
    pass


class RunLock:
    """Exclusive per-run lock so cron and the TUI can't run the same month twice."""

    def __init__(self, path: Path):
        self.path = path
        self._fh = None

    def __enter__(self) -> "RunLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+")
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fh.seek(0)
            holder = fh.read().strip() or "another process"
            fh.close()
            raise RunLocked(f"run is already in progress ({holder}); lock file: {self.path}") from None
        fh.seek(0)
        fh.truncate()
        fh.write(f"pid {os.getpid()}")
        fh.flush()
        self._fh = fh
        return self

    def __exit__(self, *exc) -> None:
        assert self._fh is not None
        self._fh.truncate(0)
        fcntl.flock(self._fh, fcntl.LOCK_UN)
        self._fh.close()
        self._fh = None
