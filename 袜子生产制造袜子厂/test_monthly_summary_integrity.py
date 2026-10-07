"""Regressions for the September stale-summary incident; no business writes."""
import sys
import unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / '袜子生产制造袜子厂'))
sys.path.insert(0, str(ROOT / 'scripts/production-parallel'))
import deterministic_upload as uploader
from guard_production_ledger import check_monthly_summary, records, check
from production_summary_guard import check_daily_summary
from unittest.mock import patch
import commit_guard

LEDGER = ROOT / '袜子生产制造袜子厂/库存记录/2026下半年下机白胚半成品统计.md'
CORRECT = '| C｜李鸿玉 | 烤边 | 53100 | 61500 | 5800 | 0 | 37500 | 0 | 157900 | 11日 | 2026-09-30 |'
STALE = '| C｜李鸿玉 | 烤边 | 57900 | 68500 | 6400 | 0 | 40600 | 0 | 173400 | 11日 | 2026-09-30 |'

class MonthlyIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.text = LEDGER.read_text()
        self.assertIn(CORRECT, self.text)

    def test_actual_stale_september_row_is_blocked_by_upload_and_daily_guards(self):
        broken = self.text.replace(CORRECT, STALE, 1)
        for validator in (check_monthly_summary, check_daily_summary):
            with self.assertRaisesRegex(ValueError, '2026年9月月度汇总 李鸿玉'):
                validator(broken)
        with self.assertRaisesRegex(ValueError, '2026年9月月度汇总 李鸿玉'):
            check(self.text, broken)

    def test_october_coverage_refresh_repairs_september_without_changing_records(self):
        broken = self.text.replace(CORRECT, STALE, 1)
        repaired = uploader._update_coverage(broken, 'A', set(), '2026-10-07')
        self.assertIn(CORRECT, repaired)
        self.assertNotIn(STALE, repaired)
        self.assertEqual(records(broken), records(repaired))
        check_daily_summary(repaired)
        self.assertEqual(repaired, uploader._update_coverage(repaired, 'A', set(), '2026-10-07'))

    def test_unrelated_october_status_upload_repairs_old_month(self):
        broken = self.text.replace(CORRECT, STALE, 1)
        report = dict(worker='A', name='徐超超', process='下机', production_date='2026-10-02',
                      values={p:0 for p in uploader.PRODUCTS}, status_only=True, not_worked=True)
        repaired = uploader.build_candidate(broken, report, 'om_fixture_source', 'om_fixture_confirm')
        self.assertIn(CORRECT, repaired)
        self.assertEqual(records(broken), records(repaired))
        check(self.text, repaired)
        check_daily_summary(repaired)

    def test_real_preupload_guard_rejects_wrong_monthly_and_daily_views(self):
        local = str(ROOT / '袜子生产制造袜子厂/guard_production_ledger.py')
        with patch.object(commit_guard, 'LEDGER_GUARD_PATH', local):
            commit_guard.run_original_ledger_guard(self.text, self.text)
            with self.assertRaisesRegex(ValueError, '月度汇总'):
                commit_guard.run_original_ledger_guard(self.text, self.text.replace(CORRECT, STALE, 1))
            old = '| C｜李鸿玉 | 烤边 | 4800 | 6000 | 600 | 0 | 3200 | 0 | 14600 |'
            self.assertIn(old, self.text)
            with self.assertRaisesRegex(ValueError, '每日汇总'):
                commit_guard.run_original_ledger_guard(self.text, self.text.replace(old, old.replace('14600', '14601'), 1))

    def test_wrong_product_count_latest_and_missing_worker_are_blocked(self):
        variants = [CORRECT.replace('53100', '53101'), CORRECT.replace('11日', '12日'),
                    CORRECT.replace('2026-09-30', '2026-09-29'), '']
        for replacement in variants:
            with self.subTest(replacement=replacement), self.assertRaises(ValueError):
                check_monthly_summary(self.text.replace(CORRECT, replacement, 1))

    def test_revoking_last_workday_makes_month_empty_without_zero_records(self):
        revised = self.text
        for key, record in records(self.text).items():
            if record.code == 'D':
                old = f'| {key} | {record.product} | {record.quantity} | {record.status} |'
                revised = revised.replace(old, old.replace(record.status, '已撤销（测试夹具）'))
        repaired = uploader.rebuild_monthly_summaries(revised)
        self.assertIn('| D｜张小翠 | 烤边 | — | — | — | — | — | — | — | 0日 | — |', repaired)
        self.assertEqual(records(revised), records(repaired))
        check_monthly_summary(repaired)

    def test_removing_all_monthly_sections_is_blocked(self):
        import re
        broken = re.sub(r'^## [^\n]*月月度汇总\s*$', '## 测试删除月份标题', self.text, flags=re.M)
        with self.assertRaisesRegex(ValueError, '缺少月度汇总区'):
            check_monthly_summary(broken)

    def test_empty_2027_template_remains_empty_and_valid(self):
        text = (ROOT / 'scripts/production-parallel/templates/2027全年下机白胚半成品统计.md').read_text()
        regenerated = uploader.rebuild_monthly_summaries(text)
        check_monthly_summary(regenerated)
        self.assertEqual(records(text), records(regenerated))

if __name__ == '__main__':
    unittest.main()
