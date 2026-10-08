#!/usr/bin/env python3
"""Record the TUI running in a pseudoterminal and render it to MP4.

    python scripts/record_demo.py [--root DIR] [--out docs/demo.mp4]

What it does:
  1. Creates 10 fake files of varying sizes under ROOT/source.
  2. Starts `archiver tui` in a real pty (120x32, xterm-256color), presses
     `r`, waits for the run to finish, then presses `q`. Every byte the app
     writes is recorded with a timestamp (saved as an asciicast v2 .cast,
     playable with `asciinema play`).
  3. Replays that byte stream through a terminal emulator (pyte) and draws
     each frame with Pillow, piping frames to ffmpeg.

The stages are paced (sleeps added in progress callbacks, in the recorded
child process only) so the whole run fits a 15-second video; real runs go
as fast as the disks and network allow. If `rsync` isn't installed, the
test suite's stand-in (tests/fake_rsync.py) is put on PATH as `rsync`.

Needs: textual, pyte, Pillow, ffmpeg with libx264.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import pty
import random
import select
import shutil
import signal
import struct
import subprocess
import sys
import termios
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
COLS, ROWS = 120, 32
FPS = 30
DURATION = 15.0
PRESS_RUN_AT = 1.5          # seconds of idle UI before pressing r
PACE = {"checksum": 2.4, "archive": 2.8, "transfer": 2.8, "verify": 2.0}
RUN_ID = "2026-09"

FILES = [  # (relative path, size in bytes)
    ("invoices/2026-09-02_acme.pdf", 48_213),
    ("invoices/2026-09-17_globex.pdf", 112_904),
    ("notes/meeting-minutes.txt", 6_412),
    ("photos/site-visit-001.jpg", 2_871_330),
    ("photos/site-visit-002.jpg", 3_402_118),
    ("photos/site-visit-003.jpg", 4_116_771),
    ("scans/contract-signed.tiff", 9_840_256),
    ("exports/ledger-2026-09.csv", 1_205_632),
    ("backups/db-dump.sql.gz", 14_662_017),
    ("video/walkthrough.mp4", 22_530_048),
]


# ---------------------------------------------------------------- setup

def make_files(root: Path) -> Path:
    source = root / "source"
    rng = random.Random(2026)
    for rel, size in FILES:
        path = source / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if rel.endswith((".txt", ".csv")):  # compressible text
            line = b"2026-09,ACME-%05d,INV,1250.00,paid\n"
            data = b"".join(line % i for i in range(size // len(line) + 1))[:size]
        else:
            data = rng.randbytes(size)
        path.write_bytes(data)
    return source


def rsync_shim(root: Path) -> Path | None:
    if shutil.which("rsync"):
        return None
    bindir = root / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    shim = bindir / "rsync"
    shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{REPO / "tests" / "fake_rsync.py"}" "$@"\n')
    shim.chmod(0o755)
    return bindir


# ---------------------------------------------------------------- child

def child_main(argv: list[str]) -> None:
    """Runs inside the pty: pace the stages, then start the real CLI."""
    from archiver import state as state_mod
    from archiver.cli import main

    original = state_mod.StateStore.set_progress
    started: dict[str, float] = {}

    def paced(self, state, name, current, total, detail):
        original(self, state, name, current, total, detail)
        budget = PACE.get(name)
        if budget and total:
            t0 = started.setdefault(name, time.monotonic())
            lag = t0 + budget * current / total - time.monotonic()
            if lag > 0:
                time.sleep(lag)

    state_mod.StateStore.set_progress = paced
    raise SystemExit(main(argv))


# --------------------------------------------------------------- record

def record(root: Path, cast_path: Path) -> list[tuple[float, bytes]]:
    source = make_files(root)
    shim_dir = rsync_shim(root)
    state_dir, work_dir, nas = root / "state", root / "work", root / "nas"
    for d in (state_dir, work_dir, nas):
        shutil.rmtree(d, ignore_errors=True)
    nas.mkdir(parents=True)

    env = dict(os.environ, TERM="xterm-256color", COLORTERM="truecolor", COLUMNS=str(COLS), LINES=str(ROWS))
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(REPO / "src"), env.get("PYTHONPATH")]))
    if shim_dir:
        env["PATH"] = f"{shim_dir}{os.pathsep}{env['PATH']}"
    args = ["--state-dir", str(state_dir), "tui", str(source), f"{nas}/",
            "--run-id", RUN_ID, "--work-dir", str(work_dir)]

    pid, fd = pty.fork()
    if pid == 0:
        os.execvpe(sys.executable, [sys.executable, __file__, "--child", *args], env)

    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLS, 0, 0))
    events: list[tuple[float, bytes]] = []
    t0: float | None = None
    pressed_run = pressed_quit = False
    state_file = state_dir / f"{RUN_ID}.json"

    def run_done() -> bool:
        try:
            stages = json.loads(state_file.read_text())["stages"]
        except (OSError, ValueError, KeyError):
            return False
        return all(s["status"] == "done" for s in stages)

    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        r, _, _ = select.select([fd], [], [], 0.02)
        if r:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            now = time.monotonic()
            t0 = t0 or now
            events.append((now - t0, chunk))
        if t0 is None:
            continue
        elapsed = time.monotonic() - t0
        if not pressed_run and elapsed >= PRESS_RUN_AT:
            os.write(fd, b"r")
            pressed_run = True
        if pressed_run and not pressed_quit and elapsed >= DURATION + 1 and run_done():
            os.write(fd, b"q")
            pressed_quit = True
    else:
        os.kill(pid, signal.SIGKILL)
    os.waitpid(pid, 0)

    if not run_done():
        raise SystemExit("recording failed: the run did not complete; see the .cast file")
    if events[-1][0] < DURATION:
        raise SystemExit("recording ended early")

    with open(cast_path, "w") as fh:
        header = {"version": 2, "width": COLS, "height": ROWS, "timestamp": int(time.time()),
                  "env": {"TERM": "xterm-256color", "SHELL": "/bin/bash"},
                  "title": "archiver tui"}
        fh.write(json.dumps(header) + "\n")
        for t, data in events:
            fh.write(json.dumps([round(t, 4), "o", data.decode("utf-8", "replace")]) + "\n")
    return events


# --------------------------------------------------------------- render

ANSI = {
    "black": "000000", "red": "cd3131", "green": "0dbc79", "brown": "e5e510", "yellow": "e5e510",
    "blue": "2472c8", "magenta": "bc3fbc", "cyan": "11a8cd", "white": "e5e5e5",
    "brightblack": "666666", "brightred": "f14c4c", "brightgreen": "23d18b", "brightbrown": "f5f543",
    "brightyellow": "f5f543", "brightblue": "3b8eea", "brightmagenta": "d670d6", "brightcyan": "29b8db",
    "brightwhite": "ffffff",
}
DEFAULT_FG, DEFAULT_BG = (0xD4, 0xD4, 0xD4), (0x12, 0x12, 0x16)
# Glyphs DejaVu Sans Mono lacks, mapped to the closest one it has.
GLYPH_FALLBACK = {"⭘": "○"}


def _rgb(color: str, default: tuple[int, int, int]) -> tuple[int, int, int]:
    if color == "default":
        return default
    hexval = ANSI.get(color, color)
    try:
        return tuple(int(hexval[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
    except ValueError:
        return default


def render(events: list[tuple[float, bytes]], out: Path) -> None:
    import pyte
    from PIL import Image, ImageDraw, ImageFont

    font_dir = Path("/usr/share/fonts/truetype/dejavu")
    size = 16
    regular = ImageFont.truetype(str(font_dir / "DejaVuSansMono.ttf"), size)
    bold = ImageFont.truetype(str(font_dir / "DejaVuSansMono-Bold.ttf"), size)
    cw = round(regular.getlength("M"))
    ascent, descent = regular.getmetrics()
    ch = ascent + descent
    pad = 16
    width = (COLS * cw + 2 * pad + 1) // 2 * 2
    height = (ROWS * ch + 2 * pad + 1) // 2 * 2

    screen = pyte.Screen(COLS, ROWS)
    stream = pyte.ByteStream(screen)
    img = Image.new("RGB", (width, height), DEFAULT_BG)
    draw = ImageDraw.Draw(img)

    ffmpeg = subprocess.Popen(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
         "-s", f"{width}x{height}", "-r", str(FPS), "-i", "-",
         "-c:v", "libx264", "-preset", "slow", "-crf", "20", "-tune", "animation",
         "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)],
        stdin=subprocess.PIPE,
    )
    assert ffmpeg.stdin is not None
    i = 0
    for frame in range(int(DURATION * FPS)):
        t = frame / FPS
        while i < len(events) and events[i][0] <= t:
            stream.feed(events[i][1])
            i += 1
        for y in sorted(screen.dirty):
            if y >= ROWS:
                continue
            row = screen.buffer[y]
            top = pad + y * ch
            for x in range(COLS):
                c = row[x]
                fg, bg = _rgb(c.fg, DEFAULT_FG), _rgb(c.bg, DEFAULT_BG)
                if c.reverse:
                    fg, bg = bg, fg
                left = pad + x * cw
                draw.rectangle([left, top, left + cw - 1, top + ch - 1], fill=bg)
                if c.data and c.data != " ":
                    glyph = GLYPH_FALLBACK.get(c.data, c.data)
                    draw.text((left, top), glyph, font=bold if c.bold else regular, fill=fg)
                if c.underscore:
                    draw.line([left, top + ch - 2, left + cw - 1, top + ch - 2], fill=fg)
        screen.dirty.clear()
        ffmpeg.stdin.write(img.tobytes())
    ffmpeg.stdin.close()
    if ffmpeg.wait() != 0:
        raise SystemExit("ffmpeg failed")


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "--child":
        child_main(sys.argv[2:])
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--root", type=Path, default=Path.home() / "archiver-demo",
                   help="scratch dir for the fake files, state and NAS (default ~/archiver-demo)")
    p.add_argument("--out", type=Path, default=REPO / "docs" / "demo.mp4")
    args = p.parse_args()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    cast = args.out.with_suffix(".cast")
    events = record(args.root, cast)
    render(events, args.out)
    print(f"wrote {args.out} and {cast} ({events[-1][0]:.1f}s recorded, first {DURATION:.0f}s rendered)")


if __name__ == "__main__":
    main()
