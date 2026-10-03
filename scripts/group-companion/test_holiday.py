import datetime
import tempfile
import unittest
from pathlib import Path

from holiday import deliver


class HolidayWindowTests(unittest.TestCase):
    def test_holiday_delivery_window_ends_at_noon(self):
        calls = []
        with tempfile.TemporaryDirectory() as directory:
            result = deliver(
                Path(directory) / "holidays.sqlite",
                datetime.datetime(2026, 1, 1, 4, 0, tzinfo=datetime.timezone.utc),
                lambda text, key: calls.append((text, key)),
            )

        self.assertEqual(result, {"status": "outside_window"})
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
