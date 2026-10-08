from __future__ import annotations

import os
import shutil
import subprocess
import tarfile
import threading
import time
import unittest

from archiver import stages as st
from helpers import TmpTestCase

HAVE_MD5SUM = shutil.which("md5sum") is not None


class Collector:
    def __init__(self):
        self.progress: list[tuple[int, int, str]] = []
        self.logs: list[str] = []
        self.ctx = st.StageContext(progress=self._p, log=self.logs.append)

    def _p(self, c, t, d):
        self.progress.append((c, t, d))


class ManifestFormatTests(TmpTestCase):
    def test_escaping_round_trips(self):
        names = ["plain.txt", "back\\slash.txt", "new\nline.txt", "café/ü.txt"]
        path = self.root / "m.md5"
        path.write_text("".join(st.manifest_line("0" * 32, n) for n in names), encoding="utf-8")
        self.assertEqual(list(st.parse_manifest(path)), names)

    def test_malformed_line_rejected(self):
        path = self.root / "bad.md5"
        path.write_text("not-a-digest file\n")
        with self.assertRaises(st.StageError):
            st.parse_manifest(path)


class StageTests(TmpTestCase):
    def run_discover_checksum(self):
        cfg = self.config()
        cfg.work_dir.mkdir(parents=True)
        entries = st.discover(cfg.source_dir, cfg.filelist_path, st.StageContext())
        st.checksum(cfg.source_dir, entries, cfg.manifest_path, st.StageContext())
        return cfg, entries

    def test_discover_sorted_and_skips_symlinks(self):
        os.symlink(self.source / "a.txt", self.source / "link.txt")
        cfg = self.config()
        c = Collector()
        entries = st.discover(cfg.source_dir, cfg.filelist_path, c.ctx)
        rels = [e.rel for e in entries]
        self.assertEqual(rels, sorted(rels))
        self.assertNotIn("link.txt", rels)
        self.assertIn("sub/deeper/c with spaces.txt", rels)
        self.assertTrue(any("link.txt" in m for m in c.logs))
        self.assertEqual(st.load_filelist(cfg.filelist_path), entries)

    def test_discover_empty_source_fails(self):
        for p in sorted(self.source.rglob("*"), reverse=True):
            p.unlink() if p.is_file() else p.rmdir()
        cfg = self.config()
        with self.assertRaisesRegex(st.StageError, "no files"):
            st.discover(cfg.source_dir, cfg.filelist_path, st.StageContext())

    @unittest.skipUnless(HAVE_MD5SUM, "md5sum not installed")
    def test_manifest_is_md5sum_compatible(self):
        cfg, _ = self.run_discover_checksum()
        r = subprocess.run(["md5sum", "--quiet", "-c", str(cfg.manifest_path)], cwd=cfg.source_dir,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_checksum_reports_byte_progress(self):
        cfg = self.config()
        entries = st.discover(cfg.source_dir, cfg.filelist_path, st.StageContext())
        c = Collector()
        st.checksum(cfg.source_dir, entries, cfg.manifest_path, c.ctx)
        total = sum(e.size for e in entries)
        self.assertEqual(c.progress[-1][:2], (total, total))

    def test_change_after_discover_is_caught(self):
        cfg = self.config()
        entries = st.discover(cfg.source_dir, cfg.filelist_path, st.StageContext())
        (self.source / "a.txt").write_text("changed and longer\n")
        with self.assertRaisesRegex(st.StageError, "changed after discovery"):
            st.checksum(cfg.source_dir, entries, cfg.manifest_path, st.StageContext())
        self.assertFalse(cfg.manifest_path.exists(), "no partial manifest left behind")

    def test_archive_layout_and_roundtrip(self):
        cfg, entries = self.run_discover_checksum()
        st.archive(cfg.source_dir, entries, cfg.manifest_path, cfg.tar_path, cfg.run_id, st.StageContext())
        self.assertFalse(cfg.tar_path.with_name(cfg.tar_path.name + ".part").exists())
        with tarfile.open(cfg.tar_path) as tar:
            names = tar.getnames()
            self.assertTrue(all(n.startswith("2026-10/") for n in names))
            self.assertIn("2026-10/MANIFEST.md5", names)
            out = self.root / "restore"
            tar.extractall(out, filter="data")
        if HAVE_MD5SUM:
            r = subprocess.run(["md5sum", "--quiet", "-c", "MANIFEST.md5"], cwd=out / "2026-10",
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_uncompressed_and_xz(self):
        for comp, ext in (("none", ".tar"), ("xz", ".tar.xz")):
            cfg = self.config(compression=comp, work_dir=self.root / f"w-{comp}")
            cfg.work_dir.mkdir()
            entries = st.discover(cfg.source_dir, cfg.filelist_path, st.StageContext())
            st.checksum(cfg.source_dir, entries, cfg.manifest_path, st.StageContext())
            st.archive(cfg.source_dir, entries, cfg.manifest_path, cfg.tar_path, cfg.run_id, st.StageContext())
            self.assertTrue(cfg.tar_path.name.endswith(ext))
            self.assertTrue(tarfile.is_tarfile(cfg.tar_path))

    def test_verify_catches_archive_manifest_mismatch(self):
        cfg, entries = self.run_discover_checksum()
        st.archive(cfg.source_dir, entries, cfg.manifest_path, cfg.tar_path, cfg.run_id, st.StageContext())
        st.transfer([cfg.tar_path, cfg.manifest_path], cfg.dest, cfg.rsync_cmd, (), st.StageContext())
        text = cfg.manifest_path.read_text().replace(
            cfg.manifest_path.read_text()[:32], "f" * 32, 1)
        cfg.manifest_path.write_text(text)
        with self.assertRaisesRegex(st.StageError, "checksum mismatch inside archive"):
            st.verify(entries, cfg.manifest_path, cfg.tar_path, cfg.run_id, cfg.dest,
                      cfg.rsync_cmd, (), st.StageContext())

    def test_transfer_progress_and_verify(self):
        cfg, entries = self.run_discover_checksum()
        st.archive(cfg.source_dir, entries, cfg.manifest_path, cfg.tar_path, cfg.run_id, st.StageContext())
        c = Collector()
        st.transfer([cfg.tar_path, cfg.manifest_path], cfg.dest, cfg.rsync_cmd, (), c.ctx)
        pcts = [p for p, total, _ in c.progress if total == 100]
        self.assertEqual(pcts[-1], 100)
        self.assertTrue(any(0 < p < 100 for p in pcts), f"expected intermediate progress, got {pcts}")
        self.assertTrue((self.nas / cfg.tar_path.name).exists())
        self.assertTrue((self.nas / cfg.manifest_path.name).exists())
        st.verify(entries, cfg.manifest_path, cfg.tar_path, cfg.run_id, cfg.dest, cfg.rsync_cmd, (), st.StageContext())

    def test_transfer_failure_reports_exit_code(self):
        cfg, entries = self.run_discover_checksum()
        os.environ["FAKE_RSYNC_FAIL"] = "1"
        with self.assertRaisesRegex(st.StageError, "code 23"):
            st.transfer([cfg.manifest_path], cfg.dest, cfg.rsync_cmd, (), st.StageContext())

    def test_missing_rsync_is_a_clear_error(self):
        cfg, _ = self.run_discover_checksum()
        with self.assertRaisesRegex(st.StageError, "rsync not found"):
            st.transfer([cfg.manifest_path], cfg.dest, ("/nonexistent/rsync",), (), st.StageContext())

    def test_cancel_stops_transfer_promptly(self):
        cfg, entries = self.run_discover_checksum()
        st.archive(cfg.source_dir, entries, cfg.manifest_path, cfg.tar_path, cfg.run_id, st.StageContext())
        os.environ["FAKE_RSYNC_SLEEP"] = "0.5"
        ctx = st.StageContext()
        threading.Timer(0.3, ctx.cancel.set).start()
        t0 = time.monotonic()
        with self.assertRaises(st.StageCancelled):
            st.transfer([cfg.tar_path], cfg.dest, cfg.rsync_cmd, (), ctx)
        self.assertLess(time.monotonic() - t0, 5)

    def test_cancel_during_checksum(self):
        cfg = self.config()
        entries = st.discover(cfg.source_dir, cfg.filelist_path, st.StageContext())
        ctx = st.StageContext()
        ctx.cancel.set()
        with self.assertRaises(st.StageCancelled):
            st.checksum(cfg.source_dir, entries, cfg.manifest_path, ctx)


if __name__ == "__main__":
    unittest.main()
