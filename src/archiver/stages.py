"""The archival work itself, one function per stage.

Nothing here knows about the TUI. Each stage takes a ``StageContext`` that
carries a progress callback, a log callback and a cancel flag, so any
frontend (Textual, plain console, tests) can drive it.

Stages communicate only through files in the work dir, never through memory,
so any stage can be resumed by a fresh process:

    discover -> filelist.json        (relative path, size, mtime per file)
    checksum -> <run_id>.md5         (md5sum-compatible manifest)
    archive  -> <run_id>.tar.gz      (files + MANIFEST.md5 under <run_id>/)
    transfer -> tarball + manifest copied to the rsync destination
    verify   -> tar contents re-hashed against the manifest, and the remote
                copy compared with rsync --checksum
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import stat
import subprocess
import tarfile
import threading
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from .util import atomic_write_bytes

CHUNK = 1 << 20
MANIFEST_ARCNAME = "MANIFEST.md5"

ProgressFn = Callable[[int, int, str], None]
LogFn = Callable[[str], None]


def _noop(*_a, **_kw) -> None:
    pass


class StageError(RuntimeError):
    """An expected, explainable failure. The message is shown to the user."""


class StageCancelled(RuntimeError):
    """The user asked to stop. The stage can be resumed later."""


@dataclass
class StageContext:
    progress: ProgressFn = _noop
    log: LogFn = _noop
    cancel: threading.Event = field(default_factory=threading.Event)

    def check_cancel(self) -> None:
        if self.cancel.is_set():
            raise StageCancelled("cancelled")


@dataclass(frozen=True)
class FileEntry:
    rel: str            # POSIX path relative to the source dir
    size: int
    mtime_ns: int


# ----------------------------------------------------------------- helpers

def _md5() -> "hashlib._Hash":
    # usedforsecurity=False keeps this working on FIPS-mode systems; md5 here
    # is an integrity checksum, not a security control.
    return hashlib.md5(usedforsecurity=False)


def _check_unchanged(path: Path, entry: FileEntry) -> None:
    try:
        st = path.stat()
    except FileNotFoundError:
        raise StageError(f"{entry.rel} was deleted after discovery; reset the run to re-scan") from None
    if st.st_size != entry.size or st.st_mtime_ns != entry.mtime_ns:
        raise StageError(f"{entry.rel} changed after discovery; reset the run to re-scan")


def manifest_line(digest: str, rel: str) -> str:
    """Format one line exactly as GNU md5sum does, including its escaping."""
    if any(c in rel for c in "\\\n\r"):
        esc = rel.replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r")
        return f"\\{digest}  {esc}\n"
    return f"{digest}  {rel}\n"


def _unescape(name: str) -> str:
    out, i = [], 0
    while i < len(name):
        c = name[i]
        if c == "\\" and i + 1 < len(name):
            out.append({"\\": "\\", "n": "\n", "r": "\r"}.get(name[i + 1], name[i + 1]))
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def parse_manifest(path: Path) -> dict[str, str]:
    """Read an md5sum-format manifest into {relative path: hex digest}."""
    result: dict[str, str] = {}
    text = path.read_text(encoding="utf-8", errors="surrogateescape")
    for lineno, line in enumerate(text.split("\n"), start=1):
        if not line:
            continue
        escaped = line.startswith("\\")
        if escaped:
            line = line[1:]
        digest, sep, name = line.partition("  ")
        if not sep or len(digest) != 32:
            raise StageError(f"malformed manifest line {lineno} in {path}")
        result[_unescape(name) if escaped else name] = digest
    return result


def load_filelist(path: Path) -> list[FileEntry]:
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        raise StageError(f"file list missing ({path}); re-run discover") from None
    return [FileEntry(**f) for f in data["files"]]


# ---------------------------------------------------------------- discover

def discover(source_dir: Path, filelist_path: Path, ctx: StageContext) -> list[FileEntry]:
    """Walk the source dir and record every regular file with its size/mtime.

    Symlinks and special files are skipped (and logged), so the archive holds
    real data only. Order is sorted so manifests diff cleanly month to month.
    """
    def on_error(err: OSError) -> None:
        raise StageError(f"cannot read {err.filename}: {err.strerror}")

    entries: list[FileEntry] = []
    for root, dirs, files in os.walk(source_dir, onerror=on_error, followlinks=False):
        ctx.check_cancel()
        dirs.sort()
        for name in files:
            path = Path(root, name)
            rel = path.relative_to(source_dir).as_posix()
            st = path.lstat()
            if not stat.S_ISREG(st.st_mode):
                ctx.log(f"skipping non-regular file: {rel}")
                continue
            entries.append(FileEntry(rel, st.st_size, st.st_mtime_ns))
            if len(entries) % 200 == 0:
                ctx.progress(len(entries), 0, rel)

    if not entries:
        raise StageError(f"no files found in {source_dir}")
    if any(e.rel == MANIFEST_ARCNAME for e in entries):
        raise StageError(f"source contains a top-level {MANIFEST_ARCNAME}, which would clash with the archive manifest")

    entries.sort(key=lambda e: e.rel)
    payload = json.dumps({"files": [asdict(e) for e in entries]}, indent=0)
    atomic_write_bytes(filelist_path, payload.encode("utf-8", "surrogateescape"))
    ctx.progress(len(entries), len(entries), f"{len(entries):,} files")
    return entries


# ---------------------------------------------------------------- checksum

def checksum(source_dir: Path, entries: Sequence[FileEntry], manifest_path: Path, ctx: StageContext) -> None:
    total = sum(e.size for e in entries)
    done = 0
    lines: list[str] = []
    for entry in entries:
        path = source_dir / entry.rel
        _check_unchanged(path, entry)
        h = _md5()
        try:
            with open(path, "rb") as fh:
                while chunk := fh.read(CHUNK):
                    ctx.check_cancel()
                    h.update(chunk)
                    done += len(chunk)
                    ctx.progress(done, total, entry.rel)
        except OSError as e:
            raise StageError(f"cannot read {entry.rel}: {e.strerror}") from e
        lines.append(manifest_line(h.hexdigest(), entry.rel))
    ctx.progress(total, total, f"{len(entries):,} files hashed")
    atomic_write_bytes(manifest_path, "".join(lines).encode("utf-8", "surrogateescape"))


# ----------------------------------------------------------------- archive

class _CountingReader:
    """File wrapper that reports bytes read and honours cancellation."""

    def __init__(self, fh, on_bytes: Callable[[int], None], ctx: StageContext):
        self._fh, self._on_bytes, self._ctx = fh, on_bytes, ctx

    def read(self, n: int = -1) -> bytes:
        self._ctx.check_cancel()
        data = self._fh.read(n)
        self._on_bytes(len(data))
        return data


_TAR_MODES = {".tar": "w", ".tar.gz": "w:gz", ".tar.xz": "w:xz"}


def archive(
    source_dir: Path,
    entries: Sequence[FileEntry],
    manifest_path: Path,
    tar_path: Path,
    run_id: str,
    ctx: StageContext,
) -> None:
    """Tar every file under a ``<run_id>/`` prefix, with the manifest beside them.

    After extraction, ``cd <run_id> && md5sum -c MANIFEST.md5`` checks the data.
    The tarball is written to ``*.part`` and renamed only once complete.
    """
    suffix = next(s for s in sorted(_TAR_MODES, key=len, reverse=True) if tar_path.name.endswith(s))
    part = tar_path.with_name(tar_path.name + ".part")
    total = sum(e.size for e in entries)
    done = 0
    current = ""

    def bump(n: int) -> None:
        nonlocal done
        done += n
        ctx.progress(done, total, current)

    try:
        with tarfile.open(part, _TAR_MODES[suffix], format=tarfile.PAX_FORMAT) as tar:
            for entry in entries:
                current = entry.rel
                path = source_dir / entry.rel
                _check_unchanged(path, entry)
                info = tar.gettarinfo(str(path), arcname=f"{run_id}/{entry.rel}")
                with open(path, "rb") as fh:
                    tar.addfile(info, _CountingReader(fh, bump, ctx))
            info = tar.gettarinfo(str(manifest_path), arcname=f"{run_id}/{MANIFEST_ARCNAME}")
            with open(manifest_path, "rb") as fh:
                tar.addfile(info, fh)
        with open(part, "rb") as fh:
            os.fsync(fh.fileno())
        os.replace(part, tar_path)
    except BaseException as e:
        part.unlink(missing_ok=True)
        if isinstance(e, OSError):
            raise StageError(f"writing archive failed at {current or 'start'}: {e}") from e
        raise
    ctx.progress(total, total, tar_path.name)


# ------------------------------------------------------------------- rsync

# rsync --info=progress2 lines look like:  "  1,234,567  42%   12.34MB/s    0:00:07"
_PROGRESS2 = re.compile(r"^\s*([\d,.]+)\s+(\d{1,3})%\s+(\S+)\s+(\S+)")


def _run_rsync(cmd: Sequence[str], ctx: StageContext, on_line: Callable[[str], None]) -> int:
    """Run rsync, feeding each output line (split on CR or LF) to ``on_line``.

    Output is read on a helper thread so cancellation works even if rsync
    goes quiet (e.g. a hung network mount).
    """
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        raise StageError(f"rsync not found: {cmd[0]!r} (install rsync or pass --rsync)") from None

    lines: queue.Queue[str | None] = queue.Queue()

    def pump() -> None:
        assert proc.stdout is not None
        buf = b""
        while chunk := proc.stdout.read1(4096):
            buf += chunk
            *complete, buf = re.split(rb"[\r\n]", buf)
            for raw in complete:
                lines.put(raw.decode(errors="replace"))
        if buf:
            lines.put(buf.decode(errors="replace"))
        lines.put(None)

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    try:
        while True:
            if ctx.cancel.is_set():
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                raise StageCancelled("cancelled")
            try:
                line = lines.get(timeout=0.2)
            except queue.Empty:
                continue
            if line is None:
                break
            if line.strip():
                on_line(line)
        return proc.wait()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        reader.join(timeout=5)
        if proc.stdout is not None:
            proc.stdout.close()


def transfer(
    files: Sequence[Path],
    dest: str,
    rsync_cmd: Sequence[str],
    rsync_opts: Sequence[str],
    ctx: StageContext,
) -> None:
    """Copy the tarball and manifest to the destination with rsync.

    ``--partial`` keeps a half-sent tarball on the NAS so a resumed transfer
    picks up where it stopped instead of starting over.
    """
    cmd = [*rsync_cmd, "-a", "--partial", "--info=progress2", *rsync_opts, *map(str, files), dest]
    ctx.log("$ " + " ".join(cmd))
    tail: deque[str] = deque(maxlen=8)

    def on_line(line: str) -> None:
        m = _PROGRESS2.match(line)
        if m:
            ctx.progress(int(m.group(2)), 100, f"{m.group(3)}  eta {m.group(4)}")
        else:
            tail.append(line.strip())
            ctx.log(line.strip())

    ctx.progress(0, 100, "starting rsync")
    code = _run_rsync(cmd, ctx, on_line)
    if code != 0:
        detail = "; ".join(tail) or "no output"
        raise StageError(f"rsync exited with code {code}: {detail}")
    ctx.progress(100, 100, "transfer complete")


# ------------------------------------------------------------------ verify

def verify(
    entries: Sequence[FileEntry],
    manifest_path: Path,
    tar_path: Path,
    run_id: str,
    dest: str,
    rsync_cmd: Sequence[str],
    rsync_opts: Sequence[str],
    ctx: StageContext,
) -> None:
    """Two checks:

    1. Re-read the local tarball and hash every member against the manifest,
       proving the archive holds exactly what was checksummed.
    2. Ask rsync to compare the remote copies by checksum (dry run). Any
       itemized output means the NAS copy differs from the local one.
    """
    expected = parse_manifest(manifest_path)
    total = sum(e.size for e in entries)
    done = 0
    seen: set[str] = set()
    prefix = f"{run_id}/"

    try:
        with tarfile.open(tar_path, "r:*") as tar:
            for member in tar:
                ctx.check_cancel()
                if not member.isfile() or not member.name.startswith(prefix):
                    raise StageError(f"unexpected entry in archive: {member.name}")
                rel = member.name[len(prefix):]
                if rel == MANIFEST_ARCNAME:
                    continue
                if rel not in expected:
                    raise StageError(f"archive contains {rel}, which is not in the manifest")
                h = _md5()
                fh = tar.extractfile(member)
                assert fh is not None
                while chunk := fh.read(CHUNK):
                    ctx.check_cancel()
                    h.update(chunk)
                    done += len(chunk)
                    ctx.progress(done, total, f"local: {rel}")
                if h.hexdigest() != expected[rel]:
                    raise StageError(f"checksum mismatch inside archive for {rel}")
                seen.add(rel)
    except (tarfile.TarError, OSError) as e:
        raise StageError(f"cannot read archive {tar_path.name}: {e}") from e

    missing = expected.keys() - seen
    if missing:
        raise StageError(f"{len(missing)} file(s) missing from archive, e.g. {sorted(missing)[0]}")
    ctx.log(f"local archive OK: {len(seen):,} files match the manifest")

    ctx.progress(total, total, "remote: comparing checksums with rsync")
    cmd = [*rsync_cmd, "-a", "--checksum", "--dry-run", "--itemize-changes", *rsync_opts,
           str(tar_path), str(manifest_path), dest]
    ctx.log("$ " + " ".join(cmd))
    out: list[str] = []
    code = _run_rsync(cmd, ctx, lambda line: out.append(line.strip()))
    if code != 0:
        raise StageError(f"rsync comparison exited with code {code}: {'; '.join(out[-8:]) or 'no output'}")
    # Itemize codes: first char '>' / '<' / 'c' means content would be sent or
    # created; '.' means only attributes differ (common on SMB/NFS NAS mounts
    # that ignore permissions), which doesn't affect the data.
    content = [line for line in out if line[:1] in "<>ch*"]
    for line in out:
        if line not in content:
            ctx.log(f"attribute-only difference (ignored): {line}")
    if content:
        raise StageError(
            "remote copy differs from local: " + "; ".join(content[:4])
            + " -- run `archiver reset --from transfer` to re-send"
        )
    ctx.log("remote copy OK: checksums match")
    ctx.progress(total, total, "verified")
