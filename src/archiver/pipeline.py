"""Runs the stages in order against saved state. Both frontends call this."""
from __future__ import annotations

import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

from . import stages as st
from .config import RunConfig
from .state import RunLock, RunLocked, RunState, StageStatus, StateStore

STAGES: tuple[str, ...] = ("discover", "checksum", "archive", "transfer", "verify")

# What a stage's (current, total) pair counts, so frontends can render it.
STAGE_UNITS = {
    "discover": "files",
    "checksum": "bytes",
    "archive": "bytes",
    "transfer": "percent",
    "verify": "bytes",
}

STAGE_DESCRIPTIONS = {
    "discover": "List files in the source directory",
    "checksum": "md5 every file into the manifest",
    "archive": "Tar the files and manifest",
    "transfer": "rsync tarball and manifest to the NAS",
    "verify": "Re-hash the tarball; compare the NAS copy",
}

__all__ = [
    "STAGES", "STAGE_UNITS", "STAGE_DESCRIPTIONS", "Event", "Pipeline",
    "ConfigMismatch", "RunLocked", "reset_run",
]


@dataclass(frozen=True)
class Event:
    kind: Literal["status", "progress", "log"]
    stage: str | None
    status: StageStatus | None = None
    current: int = 0
    total: int = 0
    detail: str = ""
    message: str = ""

    @property
    def unit(self) -> str:
        return STAGE_UNITS.get(self.stage or "", "")


EventFn = Callable[[Event], None]


class ConfigMismatch(RuntimeError):
    pass


class _Throttle:
    """Pass through at most one progress event per interval, plus the final one."""

    def __init__(self, interval: float):
        self.interval = interval
        self._last = 0.0

    def ready(self, current: int, total: int) -> bool:
        now = time.monotonic()
        final = total > 0 and current >= total
        if final or now - self._last >= self.interval:
            self._last = now
            return True
        return False


class Pipeline:
    def __init__(self, config: RunConfig, store: StateStore | None = None, progress_interval: float = 0.1):
        self.config = config
        self.store = store or StateStore(config.state_dir)
        self.progress_interval = progress_interval
        self.state = self._load_state()
        self._entries: list[st.FileEntry] | None = None

    # ------------------------------------------------------------ state

    def _load_state(self) -> RunState:
        c = self.config
        return self.store.load(c.run_id, STAGES) or RunState.new(c.run_id, str(c.source_dir), c.dest, STAGES)

    def _outputs_present(self, name: str) -> bool:
        c = self.config
        return {
            "discover": c.filelist_path.exists(),
            "checksum": c.manifest_path.exists(),
            "archive": c.tar_path.exists(),
        }.get(name, True)  # transfer/verify live on the NAS; trust the record

    def _repair(self, emit: EventFn) -> None:
        """Make saved state consistent with what's actually on disk.

        A DONE stage whose output file has gone (cache cleared, compression
        changed) is re-run along with everything after it. Anything after the
        first incomplete stage is reset, since its inputs may change.
        """
        for name in STAGES:
            stage = self.state.stages[name]
            if stage.status is StageStatus.DONE and self._outputs_present(name):
                continue
            if stage.status is StageStatus.DONE:
                emit(Event("log", name, message=f"output of {name} is missing; re-running from {name}"))
            # Keep this stage's own error visible; reset only the ones after it.
            idx = STAGES.index(name)
            for later in STAGES[idx + 1:]:
                if self.state.stages[later].status is not StageStatus.PENDING:
                    self.state.reset_from(later)
                    break
            if stage.status is StageStatus.DONE:
                self.state.reset_from(name)
            break
        self.store.save(self.state)

    def _check_config(self) -> None:
        c, s = self.config, self.state
        started = any(x.status is not StageStatus.PENDING for x in s.stages.values())
        if not started:
            s.source_dir, s.dest = str(c.source_dir), c.dest
            return
        if s.source_dir != str(c.source_dir) or s.dest != c.dest:
            raise ConfigMismatch(
                f"run {c.run_id} was started with source={s.source_dir} dest={s.dest}; "
                f"now source={c.source_dir} dest={c.dest}. "
                f"Use `archiver reset --run-id {c.run_id}` to start it over, or pick a different --run-id."
            )

    def _entries_list(self) -> list[st.FileEntry]:
        if self._entries is None:
            self._entries = st.load_filelist(self.config.filelist_path)
        return self._entries

    # -------------------------------------------------------------- run

    def run(self, on_event: EventFn | None = None, cancel: threading.Event | None = None,
            restart: bool = False) -> RunState:
        """Run every stage that isn't done yet. Raises on failure or cancel.

        Raises RunLocked if another process is running this month, ConfigMismatch
        if the saved run used a different source/dest, StageCancelled, or
        StageError (whose message says what went wrong).
        """
        emit = on_event or (lambda _e: None)
        cancel = cancel or threading.Event()

        with RunLock(self.store.lock_path_for(self.config.run_id)):
            # Re-read inside the lock: another process may have advanced the run.
            self.state = self._load_state()
            if restart:
                self.state.reset_from(STAGES[0])
            self._check_config()
            self._repair(emit)
            self._entries = None

            for name in STAGES:
                s = self.state.stages[name]
                emit(Event("status", name, s.status, s.current, s.total, s.detail, s.error or ""))

            if self.state.complete:
                emit(Event("log", None, message=f"run {self.config.run_id} is already complete"))
                return self.state

            for name in STAGES:
                if self.state.stages[name].status is StageStatus.DONE:
                    continue
                self._run_stage(name, emit, cancel)

            emit(Event("log", None, message=f"run {self.config.run_id} complete"))
            return self.state

    def _run_stage(self, name: str, emit: EventFn, cancel: threading.Event) -> None:
        throttle = _Throttle(self.progress_interval)

        def progress(current: int, total: int, detail: str) -> None:
            self.store.set_progress(self.state, name, current, total, detail)
            if throttle.ready(current, total):
                emit(Event("progress", name, StageStatus.RUNNING, current, total, detail))

        def log(message: str) -> None:
            emit(Event("log", name, message=message))

        ctx = st.StageContext(progress=progress, log=log, cancel=cancel)
        self.store.set_status(self.state, name, StageStatus.RUNNING)
        emit(Event("status", name, StageStatus.RUNNING))

        def finish(status: StageStatus, error: str | None = None) -> None:
            self.store.set_status(self.state, name, status, error=error)
            s = self.state.stages[name]
            emit(Event("status", name, status, s.current, s.total, s.detail, error or ""))

        try:
            self._dispatch(name, ctx)
        except st.StageCancelled:
            finish(StageStatus.CANCELLED, "cancelled by user")
            raise
        except st.StageError as e:
            finish(StageStatus.FAILED, str(e))
            raise
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            finish(StageStatus.FAILED, msg)
            raise st.StageError(msg) from e
        except BaseException:  # KeyboardInterrupt etc.: record it, then propagate
            finish(StageStatus.CANCELLED, "interrupted")
            raise
        finish(StageStatus.DONE)

    def _dispatch(self, name: str, ctx: st.StageContext) -> None:
        c = self.config
        c.work_dir.mkdir(parents=True, exist_ok=True)
        if name == "discover":
            self._entries = st.discover(c.source_dir, c.filelist_path, ctx)
        elif name == "checksum":
            st.checksum(c.source_dir, self._entries_list(), c.manifest_path, ctx)
        elif name == "archive":
            st.archive(c.source_dir, self._entries_list(), c.manifest_path, c.tar_path, c.run_id, ctx)
        elif name == "transfer":
            st.transfer([c.tar_path, c.manifest_path], c.dest, c.rsync_cmd, c.rsync_opts, ctx)
        elif name == "verify":
            st.verify(self._entries_list(), c.manifest_path, c.tar_path, c.run_id,
                      c.dest, c.rsync_cmd, c.rsync_opts, ctx)
        else:  # pragma: no cover
            raise st.StageError(f"unknown stage {name}")


def reset_run(state_dir: Path, run_id: str, from_stage: str | None = None,
              purge_dir: Path | None = None) -> RunState | None:
    """Mark ``from_stage`` (default: all) and later stages pending.

    With ``purge_dir`` set, also delete that work directory. Refuses while a
    run holds the lock. Returns the updated state, or None if no run exists.
    """
    store = StateStore(state_dir)
    with RunLock(store.lock_path_for(run_id)):
        state = store.load(run_id, STAGES)
        if state is not None:
            state.reset_from(from_stage or STAGES[0])
            store.save(state)
        if purge_dir is not None and purge_dir.exists():
            shutil.rmtree(purge_dir)
        return state
