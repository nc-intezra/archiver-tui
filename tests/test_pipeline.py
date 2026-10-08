from __future__ import annotations

import json
import os
import unittest

from archiver import stages as st
from archiver.pipeline import STAGES, ConfigMismatch, Pipeline, reset_run
from archiver.state import RunLock, RunLocked, StageStatus, StateStore
from helpers import REAL_RSYNC, TmpTestCase


def statuses(pipeline: Pipeline) -> dict[str, str]:
    return {n: s.status.value for n, s in pipeline.state.stages.items()}


class PipelineTests(TmpTestCase):
    def test_full_run(self):
        events = []
        p = Pipeline(self.config())
        p.run(on_event=events.append)
        self.assertEqual(set(statuses(p).values()), {"done"})
        self.assertTrue((self.nas / "2026-10.tar.gz").exists())
        self.assertTrue((self.nas / "2026-10.md5").exists())
        saved = json.loads((self.state_dir / "2026-10.json").read_text())
        self.assertTrue(all(s["status"] == "done" for s in saved["stages"]))
        kinds = {e.kind for e in events}
        self.assertEqual(kinds, {"status", "progress", "log"})

    def test_second_run_is_a_no_op(self):
        Pipeline(self.config()).run()
        events = []
        Pipeline(self.config()).run(on_event=events.append)
        self.assertTrue(any("already complete" in e.message for e in events))

    def test_resume_after_transfer_failure_skips_done_stages(self):
        os.environ["FAKE_RSYNC_FAIL"] = "1"
        p = Pipeline(self.config())
        with self.assertRaises(st.StageError):
            p.run()
        self.assertEqual(statuses(p)["transfer"], "failed")
        self.assertEqual(statuses(p)["verify"], "pending")
        checksum_finished = p.state.stages["checksum"].finished_at

        del os.environ["FAKE_RSYNC_FAIL"]
        p2 = Pipeline(self.config())  # fresh process, same saved state
        self.assertIn("code 23", p2.state.stages["transfer"].error)
        p2.run()
        self.assertEqual(set(statuses(p2).values()), {"done"})
        self.assertEqual(p2.state.stages["checksum"].finished_at, checksum_finished)

    def test_missing_tarball_reruns_from_archive(self):
        cfg = self.config()
        Pipeline(cfg).run()
        before = Pipeline(cfg).state.stages["checksum"].finished_at
        cfg.tar_path.unlink()
        p = Pipeline(cfg)
        p.run()
        self.assertTrue(cfg.tar_path.exists())
        self.assertEqual(p.state.stages["checksum"].finished_at, before)

    def test_crashed_running_stage_loads_as_failed(self):
        cfg = self.config()
        store = StateStore(cfg.state_dir)
        p = Pipeline(cfg, store=store)
        store.set_status(p.state, "discover", StageStatus.RUNNING)
        reloaded = Pipeline(cfg)
        self.assertEqual(reloaded.state.stages["discover"].status, StageStatus.FAILED)
        self.assertIn("interrupted", reloaded.state.stages["discover"].error)

    def test_concurrent_run_is_refused(self):
        cfg = self.config()
        store = StateStore(cfg.state_dir)
        with RunLock(store.lock_path_for(cfg.run_id)):
            with self.assertRaises(RunLocked):
                Pipeline(cfg).run()

    def test_changed_destination_is_refused_until_restart(self):
        Pipeline(self.config()).run()
        other = self.config()
        other = type(other).build(self.source, str(self.root / "elsewhere") + "/", run_id="2026-10",
                                  work_dir=self.work, state_dir=self.state_dir, rsync=self.rsync)
        with self.assertRaises(ConfigMismatch):
            Pipeline(other).run()
        Pipeline(other).run(restart=True)
        self.assertTrue((self.root / "elsewhere" / "2026-10.tar.gz").exists())

    def test_tampered_remote_copy_fails_verify(self):
        cfg = self.config()
        Pipeline(cfg).run()
        remote = self.nas / cfg.tar_path.name
        data = bytearray(remote.read_bytes())
        data[100] ^= 0xFF
        remote.write_bytes(bytes(data))
        reset_run(cfg.state_dir, cfg.run_id, "verify")
        p = Pipeline(cfg)
        with self.assertRaisesRegex(st.StageError, "remote copy differs"):
            p.run()
        self.assertEqual(statuses(p)["verify"], "failed")

        reset_run(cfg.state_dir, cfg.run_id, "transfer")
        Pipeline(cfg).run()

    def test_reset_with_purge(self):
        cfg = self.config()
        Pipeline(cfg).run()
        reset_run(cfg.state_dir, cfg.run_id, purge_dir=cfg.work_dir)
        self.assertFalse(cfg.work_dir.exists())
        self.assertEqual(set(statuses(Pipeline(cfg)).values()), {"pending"})

    def test_cancel_marks_stage_cancelled_and_resumes(self):
        cfg = self.config()
        p = Pipeline(cfg)
        cancel = __import__("threading").Event()

        def on_event(ev):
            if ev.kind == "status" and ev.stage == "archive" and ev.status is StageStatus.RUNNING:
                cancel.set()

        with self.assertRaises(st.StageCancelled):
            p.run(on_event=on_event, cancel=cancel)
        self.assertEqual(statuses(p)["archive"], "cancelled")
        self.assertEqual(statuses(p)["checksum"], "done")
        Pipeline(cfg).run()

    def test_work_dir_inside_source_rejected(self):
        with self.assertRaisesRegex(ValueError, "inside the source"):
            self.config(work_dir=self.source / "work")


@unittest.skipUnless(REAL_RSYNC, "rsync not installed")
class RealRsyncTests(TmpTestCase):
    rsync = "rsync"

    def test_full_run_with_real_rsync(self):
        progress = []
        p = Pipeline(self.config())
        p.run(on_event=lambda e: e.kind == "progress" and e.stage == "transfer" and progress.append(e.current))
        self.assertEqual(set(statuses(p).values()), {"done"})
        self.assertIn(100, progress)


if __name__ == "__main__":
    unittest.main()
