"""Command line entry point.

    archiver run    SOURCE DEST [options]   headless; good for cron/systemd
    archiver tui    SOURCE DEST [options]   interactive Textual interface
    archiver status [--run-id ID]           show saved progress
    archiver reset  [--run-id ID] [--from STAGE] [--purge]

Exit codes: 0 ok, 1 a stage failed, 2 bad usage/config, 3 run locked,
130 cancelled.
"""
from __future__ import annotations

import argparse
import shutil
import signal
import sys
import threading
import time
from pathlib import Path
from typing import TextIO

from . import __version__
from .config import COMPRESSION_SUFFIX, RunConfig, default_run_id, default_state_dir, default_work_root, validate_run_id
from .pipeline import STAGES, ConfigMismatch, Event, Pipeline, reset_run
from .stages import StageCancelled, StageError
from .state import RunLocked, StageStatus, StateStore
from .util import describe_progress

EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_LOCKED, EXIT_CANCELLED = 0, 1, 2, 3, 130

ICONS = {
    StageStatus.PENDING: "·",
    StageStatus.RUNNING: "▶",
    StageStatus.DONE: "✔",
    StageStatus.FAILED: "✘",
    StageStatus.CANCELLED: "■",
}


class ConsoleReporter:
    """Prints pipeline events. Redraws one progress line on a TTY; on a
    pipe (cron, systemd journal) prints a progress line every ``every`` seconds."""

    def __init__(self, stream: TextIO = sys.stdout, every: float = 15.0):
        self.stream = stream
        self.tty = stream.isatty()
        self.every = every
        self._inline = False
        self._last = 0.0

    def _line(self, text: str) -> None:
        if self._inline:
            self.stream.write("\r\x1b[2K")
            self._inline = False
        self.stream.write(f"{time.strftime('%H:%M:%S')} {text}\n")
        self.stream.flush()

    def __call__(self, ev: Event) -> None:
        if ev.kind == "status":
            if ev.status is StageStatus.RUNNING and not ev.current:
                self._line(f"==> {ev.stage}: running")
            elif ev.status in (StageStatus.DONE, StageStatus.FAILED, StageStatus.CANCELLED):
                extra = f" - {describe_progress(ev.unit, ev.current, ev.total)}" if ev.status is StageStatus.DONE else ""
                msg = f": {ev.message}" if ev.message else ""
                self._line(f"==> {ev.stage}: {ev.status.value}{extra}{msg}")
        elif ev.kind == "log":
            self._line(f"    {ev.stage + ': ' if ev.stage else ''}{ev.message}")
        elif ev.kind == "progress":
            text = f"    {ev.stage}: {describe_progress(ev.unit, ev.current, ev.total)}  {ev.detail}"
            if self.tty:
                width = shutil.get_terminal_size((100, 20)).columns - 1
                self.stream.write("\r\x1b[2K" + text[:width])
                self.stream.flush()
                self._inline = True
            elif time.monotonic() - self._last >= self.every:
                self._last = time.monotonic()
                self._line(text.strip())


def _add_run_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("source", type=Path, help="directory whose files are archived")
    p.add_argument("dest", help="rsync destination, e.g. /mnt/nas/archives/ or nas:/volume1/archives/")
    p.add_argument("--run-id", help=f"name of this run; default is the current month ({default_run_id()})")
    p.add_argument("--work-dir", type=Path,
                   help=f"where the manifest and tarball are built (default {default_work_root()}/RUN_ID)")
    p.add_argument("--compression", choices=sorted(COMPRESSION_SUFFIX), default="gz",
                   help="tarball compression (default gz; use none for already-compressed data)")
    p.add_argument("--rsync", help="rsync command to use (default: rsync, or $ARCHIVER_RSYNC)")
    p.add_argument("--rsync-opts", default="", metavar="OPTS",
                   help="extra rsync options as one quoted string, e.g. \"-e 'ssh -p 2222' --bwlimit=50m\"")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="archiver", description="Monthly checksum -> tar -> rsync -> verify archival.")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("--state-dir", type=Path, help=f"where run state is kept (default {default_state_dir()})")
    sub = p.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run or resume the pipeline without a UI")
    _add_run_args(run)
    run.add_argument("--restart", action="store_true", help="ignore saved progress and start from discover")

    tui = sub.add_parser("tui", help="run the pipeline in the interactive terminal UI")
    _add_run_args(tui)

    status = sub.add_parser("status", help="show the saved progress of a run")
    status.add_argument("--run-id", help="run to show (default: current month)")
    status.add_argument("--all", action="store_true", help="list every known run")

    reset = sub.add_parser("reset", help="mark stages as not done so they re-run")
    reset.add_argument("--run-id", help="run to reset (default: current month)")
    reset.add_argument("--from", dest="from_stage", choices=STAGES,
                       help="first stage to re-run (default: everything)")
    reset.add_argument("--purge", action="store_true",
                       help="also delete the run's work dir (manifest, tarball)")
    reset.add_argument("--work-dir", type=Path, help="work dir to purge, if not the default")
    return p


def _config_from(args: argparse.Namespace) -> RunConfig:
    return RunConfig.build(
        args.source, args.dest, run_id=args.run_id, work_dir=args.work_dir, state_dir=args.state_dir,
        compression=args.compression, rsync=args.rsync, rsync_opts=args.rsync_opts,
    )


def cmd_run(args: argparse.Namespace) -> int:
    config = _config_from(args)
    pipeline = Pipeline(config)
    cancel = threading.Event()
    reporter = ConsoleReporter()

    def on_sigint(signum, frame):
        if cancel.is_set():
            raise KeyboardInterrupt
        cancel.set()
        print("\ncancelling after the current step (Ctrl-C again to force)...", file=sys.stderr)

    previous = {s: signal.signal(s, on_sigint) for s in (signal.SIGINT, signal.SIGTERM)}
    print(f"archiver {__version__}: run {config.run_id}  {config.source_dir} -> {config.dest}")
    try:
        pipeline.run(on_event=reporter, cancel=cancel, restart=args.restart)
        return EXIT_OK
    except (StageCancelled, KeyboardInterrupt):
        print("cancelled; run the same command again to resume", file=sys.stderr)
        return EXIT_CANCELLED
    except StageError as e:
        print(f"FAILED: {e}\nfix the cause and run the same command again to resume", file=sys.stderr)
        return EXIT_FAILED
    finally:
        for s, h in previous.items():
            signal.signal(s, h)


def cmd_tui(args: argparse.Namespace) -> int:
    try:
        from .tui import ArchiverApp
    except ImportError as e:
        print(f"the TUI needs Textual: pip install 'archiver-tui[tui]' ({e})", file=sys.stderr)
        return EXIT_USAGE
    return ArchiverApp(_config_from(args)).run() or EXIT_OK


def format_status(store: StateStore, run_id: str) -> str:
    from .pipeline import STAGE_UNITS
    state = store.load(run_id, STAGES)
    if state is None:
        return f"no saved state for run {run_id} in {store.state_dir}"
    lines = [f"run {state.run_id}   {state.source_dir} -> {state.dest}"]
    for s in state.stages.values():
        prog = describe_progress(STAGE_UNITS[s.name], s.current, s.total) if (s.current or s.total) else ""
        when = s.finished_at or s.started_at or ""
        lines.append(f"  {ICONS[s.status]} {s.name:<9} {s.status.value:<10} {prog:<34} {when}")
        if s.error:
            lines.append(f"      error: {s.error}")
    return "\n".join(lines)


def cmd_status(args: argparse.Namespace) -> int:
    store = StateStore(args.state_dir or default_state_dir())
    if args.all:
        runs = store.list_runs()
        if not runs:
            print(f"no runs recorded in {store.state_dir}")
        for run_id in runs:
            print(format_status(store, run_id), end="\n\n")
        return EXIT_OK
    print(format_status(store, validate_run_id(args.run_id or default_run_id())))
    return EXIT_OK


def cmd_reset(args: argparse.Namespace) -> int:
    run_id = validate_run_id(args.run_id or default_run_id())
    purge = (args.work_dir or default_work_root() / run_id) if args.purge else None
    state = reset_run(args.state_dir or default_state_dir(), run_id, args.from_stage, purge)
    if state is None:
        print(f"no saved state for run {run_id}")
    else:
        print(f"run {run_id}: {args.from_stage or 'all stages'} onward marked pending")
    if purge:
        print(f"removed work dir {purge}")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handler = {"run": cmd_run, "tui": cmd_tui, "status": cmd_status, "reset": cmd_reset}[args.command]
    try:
        return handler(args)
    except (ValueError, ConfigMismatch) as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_USAGE
    except RunLocked as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_LOCKED


if __name__ == "__main__":
    raise SystemExit(main())
