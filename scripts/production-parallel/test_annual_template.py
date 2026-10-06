"""Validate the reusable blank annual template and its status regeneration."""
import calendar
import re
import unittest
from pathlib import Path

import deterministic_upload
import missing_alerts


TEMPLATE = Path(__file__).with_name('templates') / '2027全年下机白胚半成品统计.md'


class AnnualTemplateTests(unittest.TestCase):
    def test_blank_year_has_all_months_without_inherited_production(self):
        text = TEMPLATE.read_text(encoding='utf-8')
        months = re.findall(r'^## [^\n]*2027年(\d{1,2})月月度汇总$', text, re.M)
        self.assertEqual([int(month) for month in months], list(range(1, 13)))
        self.assertFalse(re.search(r'20\d{6}-[ABCD]-\d{3}|om_[a-zA-Z0-9]+', text))
        for month in range(1, 13):
            self.assertIn(f'id="daily-2027-{month:02d}"', text)
            self.assertIn(f'### 2027年{month}月每日汇总', text)
            last = calendar.monthrange(2027, month)[1]
            self.assertIn(f'id="days-2027-{month:02d}-21-{last}"', text)
        self.assertEqual(missing_alerts.date_map_from_ledger(text),
                         {code: set() for code in 'ABCD'})

    def test_status_refresh_preserves_annual_features_and_empty_business_state(self):
        text = TEMPLATE.read_text(encoding='utf-8')
        candidate = deterministic_upload._update_coverage(
            text, 'A', set(), None, sync_time='2027-01-01 12:00',
        )
        for marker in ('## 手机与电脑实时查询', '?year=2027', '每月 1 号和 16 号',
                       '| 确认来源 | 处理说明 |', '不把下机和烤边相加'):
            self.assertIn(marker, candidate)
        self.assertEqual(candidate.count('## 人员生产记录覆盖情况'), 1)
        self.assertEqual(candidate.count('## 三端当前版本与同步状态（自动维护）'), 1)
        self.assertIn('当前没有待核实日期', candidate)
        self.assertFalse(re.search(r'^### 2027-\d\d-\d\d｜', candidate, re.M))
        regenerated = deterministic_upload._update_coverage(
            candidate, 'A', set(), None, sync_time='2027-01-01 12:00',
        )
        self.assertEqual(regenerated, candidate)


if __name__ == '__main__':
    unittest.main()
