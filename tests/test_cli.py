from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

from archiver.state import RunState, StageStatus
from helpers import FAKE_RSYNC, TmpTestCase

SRC = Path(__file__).resolve().parent.parent / "src"


class CliTests(TmpTestCase):
    def archiver(self, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
        full_env = {**os.environ, "PYTHONPATH": str(SRC), **(env or {})}
        return subprocess.run(
            [sys.executable, "-m", "archiver", "--state-dir", str(self.state_dir), *args],
            capture_output=True, text=True, env=full_env, timeout=120,
        )

    def run_args(self, *extra: str) -> list[str]:
        return ["run", str(self.source), str(self.nas) + "/", "--run-id", "2026-10",
                "--work-dir", str(self.work), "--rsync", FAKE_RSYNC, *extra]

    def test_run_status_rerun_reset(self):
        r = self.archiver(*self.run_args())
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("verify: done", r.stdout)

        r = self.archiver("status", "--run-id", "2026-10")
        self.assertEqual(r.stdout.count("✔"), 5, r.stdout)

        r = self.archiver(*self.run_args())
        self.assertIn("already complete", r.stdout)

        r = self.archiver("reset", "--run-id", "2026-10", "--from", "transfer")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.archiver("status", "--run-id", "2026-10")
        self.assertEqual(r.stdout.count("✔"), 3, r.stdout)

    def test_failed_stage_exit_code_and_message(self):
        r = self.archiver(*self.run_args(), env={"FAKE_RSYNC_FAIL": "1"})
        self.assertEqual(r.returncode, 1)
        self.assertIn("FAILED", r.stderr)
        self.assertIn("code 23", r.stderr)
        r = self.archiver("status", "--run-id", "2026-10")
        self.assertIn("error:", r.stdout)

    def test_bad_source_is_usage_error(self):
        r = self.archiver("run", str(self.root / "nope"), str(self.nas))
        self.assertEqual(r.returncode, 2)
        self.assertIn("does not exist", r.stderr)

    def test_bad_run_id_rejected(self):
        r = self.archiver("status", "--run-id", "../etc")
        self.assertEqual(r.returncode, 2)


class StateTests(unittest.TestCase):
    def test_round_trip_and_reset(self):
        names = ["a", "b", "c"]
        s = RunState.new("2026-10", "/src", "nas:/x/", names)
        s.stages["a"].status = StageStatus.DONE
        s.stages["b"].status = StageStatus.FAILED
        s.stages["b"].error = "boom"
        back = RunState.from_dict(s.to_dict(), names)
        self.assertEqual(back.stages["b"].error, "boom")
        back.reset_from("b")
        self.assertEqual([x.status for x in back.stages.values()],
                         [StageStatus.DONE, StageStatus.PENDING, StageStatus.PENDING])

    def test_new_stage_added_in_later_version(self):
        s = RunState.new("r", "/s", "/d", ["a"])
        back = RunState.from_dict(s.to_dict(), ["a", "b"])
        self.assertEqual(list(back.stages), ["a", "b"])


if __name__ == "__main__":
    unittest.main()
