"""Focused safety checks for the series renamer."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("rename-series.py")


class RenameSeriesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        (self.path / "a.mkv").write_text("a", encoding="utf-8")
        (self.path / "b.mp4").write_text("b", encoding="utf-8")

    def run_script(self, *args):
        return subprocess.run(
            [sys.executable, str(SCRIPT), str(self.path), *args],
            text=True,
            capture_output=True,
            check=False,
        )

    def apply(self):
        result = self.run_script("Show", "--apply")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_preview_and_undo(self):
        self.assertEqual(self.run_script("Show").returncode, 0)
        self.assertFalse((self.path / "rename.log").exists())
        self.apply()
        self.assertTrue((self.path / "Show s01e01.mkv").exists())
        result = self.run_script("--undo")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.path / "a.mkv").exists())
        self.assertTrue((self.path / "b.mp4").exists())
        self.assertNotEqual(self.run_script("--undo").returncode, 0)

    def test_conflict_aborts_all(self):
        self.apply()
        (self.path / "a.mkv").write_text("other", encoding="utf-8")
        result = self.run_script("--undo")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("original name already exists", result.stderr)
        self.assertTrue((self.path / "Show s01e01.mkv").exists())
        self.assertTrue((self.path / "Show s01e02.mp4").exists())

    def test_missing_file_aborts_all(self):
        self.apply()
        (self.path / "Show s01e02.mp4").unlink()
        result = self.run_script("--undo")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("renamed file is missing", result.stderr)
        self.assertTrue((self.path / "Show s01e01.mkv").exists())

    def test_replaced_file_aborts_all(self):
        self.apply()
        renamed = self.path / "Show s01e02.mp4"
        renamed.unlink()
        renamed.write_text("replacement", encoding="utf-8")
        result = self.run_script("--undo")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("renamed file has changed", result.stderr)
        self.assertTrue((self.path / "Show s01e01.mkv").exists())

    def test_last_apply_survives_later_preview(self):
        self.apply()
        self.assertEqual(self.run_script("Other").returncode, 0)
        self.assertEqual(self.run_script("--undo").returncode, 0)

    def test_undo_targets_only_latest_apply(self):
        self.apply()
        result = self.run_script("Other", "--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_script("--undo")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.path / "Show s01e01.mkv").exists())
        self.assertFalse((self.path / "a.mkv").exists())
        self.assertNotEqual(self.run_script("--undo").returncode, 0)

    def test_old_option_interface_still_works(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--path", str(self.path),
             "--series-name", "Show", "--apply"],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.path / "Show s01e01.mkv").exists())

    def test_apply_and_undo_are_exclusive(self):
        result = self.run_script("--apply", "--undo")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not allowed with argument", result.stderr)


if __name__ == "__main__":
    unittest.main()
