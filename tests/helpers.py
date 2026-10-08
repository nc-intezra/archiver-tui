from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from archiver.config import RunConfig

FAKE_RSYNC = f"{sys.executable} {Path(__file__).with_name('fake_rsync.py')}"
REAL_RSYNC = shutil.which("rsync")


class TmpTestCase(unittest.TestCase):
    """Gives each test a scratch tree: source/, nas/, work/, state/."""

    rsync = FAKE_RSYNC

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.source = self.root / "source"
        self.nas = self.root / "nas"
        self.work = self.root / "work"
        self.state_dir = self.root / "state"
        self.source.mkdir()
        self.make_files()
        for var in ("FAKE_RSYNC_FAIL", "FAKE_RSYNC_SLEEP"):
            os.environ.pop(var, None)

    def tearDown(self) -> None:
        for var in ("FAKE_RSYNC_FAIL", "FAKE_RSYNC_SLEEP"):
            os.environ.pop(var, None)
        self._tmp.cleanup()

    def make_files(self) -> None:
        (self.source / "a.txt").write_text("alpha\n")
        (self.source / "sub").mkdir()
        (self.source / "sub" / "b.bin").write_bytes(os.urandom(300_000))
        (self.source / "sub" / "deeper").mkdir()
        (self.source / "sub" / "deeper" / "c with spaces.txt").write_text("gamma\n")
        (self.source / "empty.dat").write_bytes(b"")

    def config(self, **kw) -> RunConfig:
        args = dict(run_id="2026-10", work_dir=self.work, state_dir=self.state_dir, rsync=self.rsync)
        args.update(kw)
        return RunConfig.build(self.source, str(self.nas) + "/", **args)
