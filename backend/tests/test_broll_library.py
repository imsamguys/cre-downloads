import tempfile
from pathlib import Path
import unittest

from cre_brain.broll_library import _candidate_windows, scan_folder


class BrollLibraryTests(unittest.TestCase):
    def test_candidates_are_three_seconds_and_stay_inside_source(self):
        windows = _candidate_windows([(0, 2), (2, 8), (8, 12)], 12)
        self.assertEqual([(0.0, 3.0), (3.5, 6.5), (8.5, 11.5)], windows)
        self.assertTrue(all(0 <= start < end <= 12 and end - start == 3 for start, end in windows))

    def test_scan_is_recursive_and_only_returns_supported_movies(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); nested = root / "movie"; nested.mkdir()
            (nested / "scene.MP4").write_bytes(b"test")
            (nested / "notes.txt").write_text("ignore", encoding="utf-8")
            self.assertEqual([nested / "scene.MP4"], scan_folder(root))


if __name__ == "__main__":
    unittest.main()
