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

sys.modules.setdefault("pandas", fake_pandas)
sys.modules.setdefault("requests", types.SimpleNamespace())

from datetime import datetime, timedelta

from run_weekly import (
    DEFAULT_DAYS_BACK,
    DEFAULT_DAYS_FORWARD,
    DEFAULT_HEADLESS,
    DEFAULT_WORKERS,
    _format_progress,
    is_meeting_date_in_window,
)


class ProgressFormattingTest(unittest.TestCase):
    def test_formats_current_total_and_percentage(self):
        self.assertEqual(_format_progress(12, 66), "[12/66 18.2%]")

    def test_defaults_to_headless_browser_and_single_worker(self):
        self.assertTrue(DEFAULT_HEADLESS)
        self.assertEqual(DEFAULT_WORKERS, 1)

    def test_default_date_window_uses_today_through_next_two_weeks(self):
        today = datetime(2026, 6, 3)

        self.assertEqual(DEFAULT_DAYS_BACK, 0)
        self.assertEqual(DEFAULT_DAYS_FORWARD, 14)
        self.assertTrue(is_meeting_date_in_window(today, today))
        self.assertTrue(is_meeting_date_in_window(today + timedelta(days=14), today))
        self.assertFalse(is_meeting_date_in_window(today - timedelta(days=1), today))
        self.assertFalse(is_meeting_date_in_window(today + timedelta(days=15), today))


if __name__ == "__main__":
    unittest.main()
