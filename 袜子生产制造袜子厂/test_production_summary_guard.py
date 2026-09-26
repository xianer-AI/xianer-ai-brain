import sys
import unittest
from pathlib import Path


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from production_summary_guard import check_daily_summary  # noqa: E402


LEDGER = HERE / "库存记录" / "2026下半年下机白胚半成品统计.md"


class ProductionSummaryGuardTests(unittest.TestCase):
    def test_current_ledger_matches_daily_summary(self):
        text = LEDGER.read_text(encoding="utf-8")
        self.assertIsNone(check_daily_summary(text))

    def test_daily_summary_rejects_wrong_quantity(self):
        text = LEDGER.read_text(encoding="utf-8")
        broken = text.replace(
            "| B｜梅芳 | 下机 | 2400 | 1600 | 200 | 0 | 0 | 0 | 4200 |",
            "| B｜梅芳 | 下机 | 2400 | 1600 | 201 | 0 | 0 | 0 | 4201 |",
            1,
        )

        with self.assertRaisesRegex(ValueError, r"2026-09-26.*B｜梅芳"):
            check_daily_summary(broken)


if __name__ == "__main__":
    unittest.main()
