import unittest
import sys
import types


class _FakeSeries:
    def __eq__(self, other):
        return self

    def tolist(self):
        return []


class _FakeFrame:
    def __getitem__(self, key):
        return _FakeSeries() if isinstance(key, str) else self


fake_pandas = types.SimpleNamespace(read_csv=lambda path: _FakeFrame())
fake_playwright_sync_api = types.SimpleNamespace(sync_playwright=lambda: None)

sys.modules.setdefault("pandas", fake_pandas)
sys.modules.setdefault("requests", types.SimpleNamespace())
sys.modules.setdefault("playwright", types.SimpleNamespace())
sys.modules.setdefault("playwright.sync_api", fake_playwright_sync_api)

from run_weekly import _format_progress


class ProgressFormattingTest(unittest.TestCase):
    def test_formats_current_total_and_percentage(self):
        self.assertEqual(_format_progress(12, 66), "[12/66 18.2%]")


if __name__ == "__main__":
    unittest.main()
