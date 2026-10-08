"""Drives the Textual app headlessly. Skipped when Textual isn't installed."""
from __future__ import annotations

import importlib.util
import unittest

from archiver.state import StageStatus
from helpers import TmpTestCase

HAVE_TEXTUAL = importlib.util.find_spec("textual") is not None


@unittest.skipUnless(HAVE_TEXTUAL, "textual not installed (pip install '.[tui]')")
class TuiTests(TmpTestCase, unittest.IsolatedAsyncioTestCase):
    async def test_run_to_completion(self):
        from archiver.tui import ArchiverApp

        app = ArchiverApp(self.config())
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.press("r")
            await app.workers.wait_for_complete()
            await pilot.pause()
            self.assertFalse(app.running)
            self.assertTrue(all(s.status is StageStatus.DONE for s in app.pipeline.state.stages.values()))
            await pilot.press("q")
        self.assertEqual(app.return_value, 0)

    async def test_reset_dialog(self):
        from archiver.tui import ArchiverApp

        app = ArchiverApp(self.config())
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.press("r")
            await app.workers.wait_for_complete()
            await pilot.pause()
            await pilot.press("x")
            await pilot.click("#yes")
            await pilot.pause()
            self.assertTrue(all(s.status is StageStatus.PENDING for s in app.pipeline.state.stages.values()))
            self.assertFalse(self.work.exists())


if __name__ == "__main__":
    unittest.main()
