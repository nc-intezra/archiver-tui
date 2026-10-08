"""Textual frontend.

Thin by design: the pipeline runs on a worker thread and reports through
``Event`` objects, which are marshalled onto the UI thread with
``call_from_thread``. All real work lives in the engine.

Keys: r run/resume · c cancel · x reset run · q quit
"""
from __future__ import annotations

import threading

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Label, Log, ProgressBar, Static

from .config import RunConfig
from .pipeline import STAGE_DESCRIPTIONS, STAGE_UNITS, STAGES, ConfigMismatch, Event, Pipeline, reset_run
from .stages import StageCancelled, StageError
from .state import RunLocked, StageStatus
from .util import describe_progress

STATUS_STYLE = {
    StageStatus.PENDING: ("·", "dim"),
    StageStatus.RUNNING: ("▶", "bold yellow"),
    StageStatus.DONE: ("✔", "bold green"),
    StageStatus.FAILED: ("✘", "bold red"),
    StageStatus.CANCELLED: ("■", "magenta"),
}


class ConfirmReset(ModalScreen[bool]):
    DEFAULT_CSS = """
    ConfirmReset { align: center middle; }
    #dialog { width: 64; height: auto; padding: 1 2; border: thick $warning; background: $surface; }
    #buttons { height: auto; margin-top: 1; align-horizontal: right; }
    #buttons Button { margin-left: 2; }
    """
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, run_id: str):
        super().__init__()
        self.run_id = run_id

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(
                f"Reset run {self.run_id}?\n\nEvery stage will be marked pending and the work "
                "directory (manifest + tarball) deleted. Files already on the NAS are left alone."
            )
            with Horizontal(id="buttons"):
                yield Button("Keep", id="no")
                yield Button("Reset", id="yes", variant="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_cancel(self) -> None:
        self.dismiss(False)


class ArchiverApp(App[int]):
    TITLE = "archiver"
    CSS = """
    #body { height: 1fr; }
    #left { width: 58; padding: 0 1; }
    #stages { height: auto; margin-bottom: 1; }
    #summary { height: auto; color: $text-muted; }
    #right { width: 1fr; padding: 0 1; }
    #current { height: 2; }
    #progress { margin-bottom: 1; }
    #log { height: 1fr; border: round $primary; }
    """
    BINDINGS = [
        Binding("r", "run", "Run / resume"),
        Binding("c", "cancel", "Cancel"),
        Binding("x", "reset", "Reset run"),
        Binding("q", "quit_app", "Quit"),
    ]

    def __init__(self, config: RunConfig):
        super().__init__()
        self.config = config
        self.pipeline = Pipeline(config)
        self.cancel_event = threading.Event()
        self.running = False
        self._quit_when_idle = False
        self.exit_code = 0

    # ------------------------------------------------------------ layout

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="body"):
            with Vertical(id="left"):
                yield DataTable(id="stages", cursor_type="none", show_cursor=False)
                yield Static(self._summary(), id="summary")
            with Vertical(id="right"):
                yield Static("Idle. Press [b]r[/b] to run or resume.", id="current")
                yield ProgressBar(id="progress", total=100, show_eta=False)
                yield Log(id="log")
        yield Footer()

    def _summary(self) -> str:
        c = self.config
        return (
            f"source   {c.source_dir}\n"
            f"dest     {c.dest}\n"
            f"work     {c.work_dir}\n"
            f"archive  {c.tar_path.name}\n"
            f"state    {c.state_dir / (c.run_id + '.json')}"
        )

    def on_mount(self) -> None:
        self.sub_title = f"run {self.config.run_id}"
        table = self.query_one("#stages", DataTable)
        table.add_column(" ", key="icon", width=1)
        table.add_column("Stage", key="stage", width=9)
        table.add_column("Status", key="status", width=9)
        table.add_column("Progress", key="progress", width=30)
        for name in STAGES:
            table.add_row("", name, "", "", key=name)
        for name, stage in self.pipeline.state.stages.items():
            self._set_row(name, stage.status, stage.current, stage.total)

        log = self.query_one("#log", Log)
        log.write_line(f"Loaded run {self.config.run_id}.")
        for name in STAGES:
            log.write_line(f"  {name:<9} {STAGE_DESCRIPTIONS[name]}")
        failed = [s for s in self.pipeline.state.stages.values() if s.error]
        for s in failed:
            log.write_line(f"Previous error in {s.name}: {s.error}")
        if self.pipeline.state.complete:
            self._set_current("This run is complete. Press [b]x[/b] to reset it.")

    # ----------------------------------------------------------- helpers

    def _set_row(self, name: str, status: StageStatus, current: int, total: int) -> None:
        icon, style = STATUS_STYLE[status]
        table = self.query_one("#stages", DataTable)
        table.update_cell(name, "icon", Text(icon, style=style))
        table.update_cell(name, "status", Text(status.value, style=style))
        prog = describe_progress(STAGE_UNITS[name], current, total) if (current or total) else ""
        table.update_cell(name, "progress", prog)

    def _set_current(self, markup: str) -> None:
        self.query_one("#current", Static).update(markup)

    # ------------------------------------------------------------ events

    def _post_event(self, ev: Event) -> None:
        """Called on the worker thread; hop to the UI thread."""
        try:
            self.call_from_thread(self._apply_event, ev)
        except RuntimeError:
            pass  # app is shutting down

    def _apply_event(self, ev: Event) -> None:
        log = self.query_one("#log", Log)
        bar = self.query_one("#progress", ProgressBar)
        if ev.kind == "log":
            log.write_line(f"{ev.stage + ': ' if ev.stage else ''}{ev.message}")
            return
        assert ev.stage is not None and ev.status is not None
        self._set_row(ev.stage, ev.status, ev.current, ev.total)
        if ev.kind == "status":
            if ev.status is StageStatus.RUNNING and not ev.current:
                log.write_line(f"==> {ev.stage}")
                self._set_current(f"[b]{ev.stage}[/b]  {STAGE_DESCRIPTIONS[ev.stage]}")
                bar.update(total=None if ev.stage == "discover" else 100, progress=0)
            elif ev.status in (StageStatus.FAILED, StageStatus.CANCELLED):
                log.write_line(f"{ev.stage}: {ev.status.value}" + (f": {ev.message}" if ev.message else ""))
        elif ev.kind == "progress":
            if ev.total:
                bar.update(total=ev.total, progress=ev.current)
            text = Text.assemble(
                (ev.stage, "bold"), "  ",
                describe_progress(ev.unit, ev.current, ev.total), "\n",
                (ev.detail, "dim"),
            )
            self.query_one("#current", Static).update(text)

    # ----------------------------------------------------------- actions

    def action_run(self) -> None:
        if self.running:
            self.notify("Already running.", severity="warning")
            return
        self.cancel_event = threading.Event()
        self.running = True
        self.run_worker(self._pipeline_worker, thread=True, exclusive=True, group="pipeline", name="pipeline")

    def _pipeline_worker(self) -> None:
        message, severity = "Unexpected error.", "error"
        try:
            self.pipeline.run(on_event=self._post_event, cancel=self.cancel_event)
            message, severity = "All stages complete.", "information"
        except StageCancelled:
            message, severity = "Cancelled. Press r to resume.", "warning"
        except StageError as e:
            message = f"Failed: {e}\nFix the cause and press r to resume."
        except (RunLocked, ConfigMismatch) as e:
            message = str(e)
        except Exception as e:  # never let a worker crash the UI silently
            message = f"Unexpected error: {type(e).__name__}: {e}"
        finally:
            try:
                self.call_from_thread(self._on_finished, message, severity)
            except RuntimeError:
                pass

    def _on_finished(self, message: str, severity: str) -> None:
        self.running = False
        self.exit_code = 0 if severity == "information" else 1
        self._set_current(message.split("\n")[0])
        self.query_one("#log", Log).write_line(message)
        self.notify(message, severity=severity, timeout=8)
        if self._quit_when_idle:
            self.exit(self.exit_code)

    def action_cancel(self) -> None:
        if not self.running:
            self.notify("Nothing is running.")
            return
        self.cancel_event.set()
        self._set_current("Cancelling after the current step...")

    def action_reset(self) -> None:
        if self.running:
            self.notify("Cancel the run before resetting it.", severity="warning")
            return

        def done(confirmed: bool | None) -> None:
            if not confirmed:
                return
            try:
                reset_run(self.config.state_dir, self.config.run_id, purge_dir=self.config.work_dir)
            except RunLocked as e:
                self.notify(str(e), severity="error")
                return
            self.pipeline = Pipeline(self.config)
            for name in STAGES:
                self._set_row(name, StageStatus.PENDING, 0, 0)
            self.query_one("#progress", ProgressBar).update(total=100, progress=0)
            self.query_one("#log", Log).write_line(f"Run {self.config.run_id} reset.")
            self._set_current("Reset. Press [b]r[/b] to start.")

        self.push_screen(ConfirmReset(self.config.run_id), done)

    def action_quit_app(self) -> None:
        if self.running:
            self._quit_when_idle = True
            self.cancel_event.set()
            self._set_current("Cancelling, then quitting...")
            return
        self.exit(self.exit_code)
