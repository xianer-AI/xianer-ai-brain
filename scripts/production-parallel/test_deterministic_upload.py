import os
import re
import unittest
from pathlib import Path
from unittest.mock import patch

import deterministic_upload as uploader


class DeterministicCandidateTests(unittest.TestCase):
    def setUp(self):
        fixture = Path('/tmp/ledger-current.md')
        if not fixture.exists():
            self.skipTest('requires the read-only ledger fixture')
        self.before = fixture.read_text(encoding='utf-8')
        self.report = {
            'worker': 'C', 'name': '李鸿玉', 'process': '烤边',
            'production_date': '2026-09-29',
            'values': {'棉堆堆袜': 4700, '冰冰袜': 6500, '小腿袜': 400,
                       '过膝袜': 0, '女船袜': 3100, '男船袜': 0},
        }

    @staticmethod
    def _personal_subtotal(text, worker, year, month):
        marker = f'### {worker}｜{year}年{month}月个人累计'
        start = text.find(marker)
        if start < 0:
            return None
        end = text.find('\n</details>', start)
        chunk = text[start:] if end < 0 else text[start:end]
        match = re.search(rf'^\| {re.escape(worker)}(?:已报|个人)小计 \| (\d+) \|', chunk, re.M)
        return None if not match else (int(match.group(1)), match.group(0), start + match.start())

    def test_confirmed_missing_product_is_coerced_to_zero_with_provenance(self):
        # The card confirmation has already authorized the normalized zero;
        # the deterministic path keeps which products were originally absent.
        inspected = {
            'source': 'om_source', 'confirmation': 'om_confirm',
            'source_record': {'result': {'extracted': {
                'worker': 'C', 'production_date': '2026-09-29',
                'items': [{'product': product, 'quantity': 1}
                          for product in uploader.PRODUCTS if product != '过膝袜'],
            }}}
        }
        with patch.object(uploader.upload_task, 'inspect', return_value=inspected):
            _, report = uploader._standard_report('/tmp/task.json')
        self.assertEqual(report['values']['过膝袜'], 0)
        self.assertEqual(report['missing_products'], {'过膝袜'})

    def test_missing_products_build_a_guard_valid_candidate(self):
        # A fixed empty template keeps this provenance test independent of
        # later real production dates.  Cumulative labels use the latest
        # recorded date, so asserting an older date on the live ledger drifts.
        template = Path(__file__).with_name('templates') / '2027全年下机白胚半成品统计.md'
        before = template.read_text(encoding='utf-8')
        report = dict(self.report, production_date='2027-01-01')
        report['missing_products'] = {'过膝袜', '男船袜'}
        candidate = uploader.build_candidate(
            before, report,
            'om_missing_source', 'om_missing_confirmation',
        )
        self.assertIn('原始未报项：过膝袜、男船袜；经员工本人本次“准确”确认按0双写入。', candidate)
        self.assertIn('| 过膝袜 | 0 | 已确认（含2027-01-01核实为0） |', candidate)
        self.assertIn('| 男船袜 | 0 | 已确认（含2027-01-01核实为0） |', candidate)
        self.assertRegex(candidate, r'(?m)^\| 20270101-C-\d{3} \| 过膝袜 \| 0 \| 已确认 \|$')
        self.assertRegex(candidate, r'(?m)^\| 20270101-C-\d{3} \| 男船袜 \| 0 \| 已确认 \|$')
        self.assertIn('来源消息：om_missing_source', candidate)
        self.assertIn('确认消息：om_missing_confirmation', candidate)

    def test_personal_subtotal_accepts_legacy_personal_label(self):
        # B's historical personal section uses ``B个人小计`` while newer
        # sections use ``B已报小计``. Both labels must remain guard-valid.
        current = self._personal_subtotal(self.before, 'C', 2026, 9)
        self.assertIsNotNone(current)
        old_total, old_row, row_offset = current
        before = self.before[:row_offset] + old_row.replace('C已报小计', 'C个人小计') + self.before[row_offset + len(old_row):]
        report = dict(self.report)
        report['worker'] = 'C'
        report['name'] = '李鸿玉'
        candidate = uploader.build_candidate(
            before, report, 'om_label_source', 'om_label_confirmation',
        )
        self.assertIn(f'| C个人小计 | {old_total + sum(report["values"].values())} | 六项均已收到数量反馈 |', candidate)

    def test_2027_template_routes_to_january_and_keeps_other_months_empty(self):
        template = Path(__file__).with_name('templates') / '2027全年下机白胚半成品统计.md'
        before = template.read_text(encoding='utf-8')
        report = {
            'worker': 'B', 'name': '梅芳', 'process': '下机',
            'production_date': '2027-01-01',
            'values': dict(zip(uploader.PRODUCTS, [1, 2, 3, 4, 5, 6])),
            'missing_products': set(),
        }
        candidate = uploader.build_candidate(before, report, 'om_2027_source', 'om_2027_confirmation')
        self.assertIn('| B｜梅芳 | 下机 | 1 | 2 | 3 | 4 | 5 | 6 | 21 | 1日 | 2027-01-01 |', candidate)
        self.assertIn('### B｜2027年1月个人累计', candidate)
        self.assertIn('### 2027年1月每日汇总', candidate)
        self.assertIn('| B｜梅芳 | 下机 | — | — | — | — | — | — | — | 0日 | — |', candidate)
        # No 2027-01 rows may appear in February's monthly section.
        def first_heading(*heads):
            return next(index for head in heads if (index := candidate.find(head)) >= 0)
        feb = candidate[first_heading('## 五、2027年2月月度汇总', '## 3、2027年2月月度汇总'):first_heading('## 六、2027年3月月度汇总', '## 4、2027年3月月度汇总')]
        self.assertNotIn('2027-01-01', feb)

    def test_validation_boundary_dates_route_to_their_own_months(self):
        """The four agreed boundary dates must never land in another month."""
        cases = [
            ('2026-10-01', '2026年10月', '20261001-B-'),
            ('2026-11-01', '2026年11月', '20261101-B-'),
            ('2026-12-01', '2026年12月', '20261201-B-'),
        ]
        for production_date, month_label, row_prefix in cases:
            with self.subTest(production_date=production_date):
                report = {
                    'worker': 'B', 'name': '梅芳', 'process': '下机',
                    'production_date': production_date,
                    'values': dict(zip(uploader.PRODUCTS, [1, 2, 3, 0, 0, 0])),
                    'missing_products': set(),
                }
                candidate = uploader.build_candidate(
                    self.before, report,
                    f'om_{production_date}_source',
                    f'om_{production_date}_confirmation',
                )
                self.assertIn(f'### B｜{month_label}个人累计', candidate)
                self.assertIn(f'### {month_label}每日汇总', candidate)
                self.assertIn(row_prefix, candidate)

    def test_2027_year_end_routes_to_december(self):
        template = Path(__file__).with_name('templates') / '2027全年下机白胚半成品统计.md'
        before = template.read_text(encoding='utf-8')
        report = {
            'worker': 'D', 'name': '张小翠', 'process': '烤边',
            'production_date': '2027-12-31',
            'values': dict(zip(uploader.PRODUCTS, [2, 0, 1, 0, 0, 0])),
            'missing_products': set(),
        }
        candidate = uploader.build_candidate(
            before, report, 'om_20271231_source', 'om_20271231_confirmation',
        )
        self.assertIn('| D｜张小翠 | 烤边 | 2 | 0 | 1 | 0 | 0 | 0 | 3 | 1日 | 2027-12-31 |', candidate)
        self.assertIn('### D｜2027年12月个人累计', candidate)
        self.assertIn('### 2027年12月每日汇总', candidate)
        def first_heading(*heads):
            return next(index for head in heads if (index := candidate.find(head)) >= 0)
        december = candidate[first_heading('## 十五、2027年12月月度汇总', '## 13、2027年12月月度汇总'):]
        self.assertIn('2027-12-31', december)
        january = candidate[first_heading('## 四、2027年1月月度汇总', '## 2、2027年1月月度汇总'):first_heading('## 五、2027年2月月度汇总', '## 3、2027年2月月度汇总')]
        self.assertNotIn('2027-12-31', january)

    def test_confirmed_non_working_status_does_not_create_production_detail(self):
        template = Path(__file__).with_name('templates') / '2027全年下机白胚半成品统计.md'
        before = template.read_text(encoding='utf-8')
        report = {
            'worker': 'C', 'name': '李鸿玉', 'process': '烤边',
            'production_date': '2027-01-02',
            'values': dict(zip(uploader.PRODUCTS, [0, 0, 0, 0, 0, 0])),
            'missing_products': set(), 'not_worked': True,
        }
        candidate = uploader.build_candidate(
            before, report, 'om_20270102_status', 'om_20270102_confirm',
        )
        self.assertNotIn('20270102-C-', candidate)
        self.assertIn('| C｜李鸿玉 | 2027年1月2日 | 已确认未上班（不计入生产统计） |', candidate)
        self.assertIn('来源消息：om_20270102_status', candidate)
        self.assertIn('确认消息：om_20270102_confirm', candidate)
        self.assertNotIn('✓ 1月2日', candidate)

    def test_numbered_update_log_keeps_status_confirmation_ids(self):
        """Choices 1/2 must remain auditable in the numbered live ledger."""
        before = self.before.replace('## 更新记录', '## 十一、更新记录')
        report = {
            'worker': 'B', 'name': '梅芳', 'process': '下机',
            'production_date': '2026-10-02',
            'values': dict(zip(uploader.PRODUCTS, [0, 0, 0, 0, 0, 0])),
            'missing_products': set(), 'not_worked': True,
        }
        candidate = uploader.build_candidate(
            before, report, 'om_numbered_status', 'om_numbered_confirm',
        )
        self.assertIn('来源消息：om_numbered_status', candidate)
        self.assertIn('确认消息：om_numbered_confirm', candidate)

    def test_ledger_endpoint_is_year_scoped(self):
        self.assertIn('2026下半年', uploader.commit_guard.endpoint_for_date('2026-12-31'))
        self.assertIn('2027全年', uploader.commit_guard.endpoint_for_date('2027-01-01'))
        with self.assertRaises(ValueError):
            uploader.commit_guard.endpoint_for_date('2028-01-01')

    def test_backfill_duplicate_date_is_blocked_before_candidate_build(self):
        text = (
            '## B｜梅芳下机\n\n'
            '### 2026-09-20｜梅芳下机\n\n'
            '| 记录编号 | 产品 | 数量（双） | 状态 |\n'
            '|---|---|---:|---|\n'
            '| 20260920-B-001 | 棉堆堆袜 | 1 | 已确认 |\n\n'
            '## C｜李鸿玉烤边\n'
        )
        report = {
            'worker': 'B', 'name': '梅芳', 'process': '下机',
            'production_date': '2026-09-20', 'backfill': True,
            'values': dict(zip(uploader.PRODUCTS, [1, 0, 0, 0, 0, 0])),
        }
        with self.assertRaisesRegex(ValueError, '禁止重复补报'):
            uploader._insert_detail(text, 'B', report, 'om_backfill_source', 'om_backfill_confirm')

    def test_backfill_placeholder_date_without_detail_is_allowed(self):
        text = (
            '## B｜梅芳下机\n\n'
            '### 2026-09-20｜梅芳\n\n'
            '当前暂无 梅芳 的有效下机记录；未报不代表零产量。\n\n'
            '<details>\n历史个人累计展示\n</details>\n\n'
            '## C｜李鸿玉烤边\n'
        )
        report = {
            'worker': 'B', 'name': '梅芳', 'process': '下机',
            'production_date': '2026-09-20', 'backfill': True,
            'values': dict(zip(uploader.PRODUCTS, [2400, 1800, 300, 0, 0, 0])),
        }
        candidate = uploader._insert_detail(
            text, 'B', report, 'om_backfill_source', 'om_backfill_confirm'
        )
        self.assertIn('20260920-B-001', candidate)

    def test_backfill_date_heading_with_unrelated_worker_does_not_block(self):
        text = (
            '## B｜梅芳下机\n\n'
            '### 2026-09-20｜梅芳\n\n'
            '当前暂无 梅芳 的有效下机记录；未报不代表零产量。\n\n'
            '## C｜李鸿玉烤边\n\n'
            '### 2026-09-20｜李鸿玉烤边\n\n'
            '| 记录编号 | 产品 | 数量（双） | 状态 |\n'
            '|---|---|---:|---|\n'
            '| 20260920-C-001 | 过膝袜 | 1 | 已确认 |\n'
        )
        report = {
            'worker': 'B', 'name': '梅芳', 'process': '下机',
            'production_date': '2026-09-20', 'backfill': True,
            'values': dict(zip(uploader.PRODUCTS, [1, 0, 0, 0, 0, 0])),
        }
        candidate = uploader._insert_detail(
            text, 'B', report, 'om_backfill_source', 'om_backfill_confirm'
        )
        self.assertIn('20260920-B-001', candidate)

    def test_backfill_detail_records_explicit_date_provenance(self):
        text = (
            '## B｜梅芳下机\n\n'
            '## C｜李鸿玉烤边\n'
        )
        report = {
            'worker': 'B', 'name': '梅芳', 'process': '下机',
            'production_date': '2026-09-20', 'backfill': True,
            'values': dict(zip(uploader.PRODUCTS, [1, 0, 0, 0, 0, 0])),
        }
        candidate = uploader._insert_detail(text, 'B', report, 'om_backfill_source', 'om_backfill_confirm')
        self.assertIn('生产日期按员工补报中明确填写的日期入账', candidate)


class ProductionStatusRegressionTests(unittest.TestCase):
    def test_numbered_update_log_keeps_status_provenance(self):
        text = (
            '## 十一、更新记录\n\n'
            '| 日期 | 更新内容 | 影响范围 | GitHub提交 |\n'
            '|---|---|---|---|\n'
            '| 2026-10-03 | 旧记录 | 状态 | abc123 |\n'
        )
        report = {
            'worker': 'B', 'name': '梅芳', 'process': '下机',
            'production_date': '2026-10-02', 'status_only': True,
            'not_worked': True, 'already_reported': False,
            'values': {product: 0 for product in uploader.PRODUCTS},
        }
        result = uploader._update_log(
            text, report, 'om_status_source', 'om_status_confirmation'
        )
        self.assertIn('来源消息：om_status_source', result)
        self.assertIn('确认消息：om_status_confirmation', result)

    def test_remote_status_reconciliation_requires_exact_bound_row(self):
        report = {
            'worker': 'B', 'name': '梅芳', 'process': '下机',
            'production_date': '2026-10-02', 'status_only': True,
            'not_worked': True, 'already_reported': False,
        }
        good = (
            '| 2026-10-02 | 确认状态 B | 梅芳（B）下机：当天未上班；'
            '来源消息：om_status_source；确认消息：om_status_confirmation |\n'
        )
        bad = (
            '| 2026-10-01 | 确认状态 B | 梅芳（B）下机：当天未上班；'
            '来源消息：om_status_source；确认消息：om_status_confirmation |\n'
        )
        self.assertTrue(uploader._remote_contains_exact_task(
            good, report, 'om_status_source', 'om_status_confirmation'
        ))
        self.assertFalse(uploader._remote_contains_exact_task(
            bad, report, 'om_status_source', 'om_status_confirmation'
        ))

if __name__ == '__main__':
    unittest.main()


class RevokedTotalsTests(unittest.TestCase):
    def test_revoked_rows_do_not_affect_totals_or_dates(self):
        text = "## C｜李鸿玉烤边\n" + "\n".join([
            "| 20260921-C-001 | 棉堆堆袜 | 4800 | 已撤销（重复） |",
            "| 20260922-C-002 | 冰冰袜 | 7000 | 已作废 |",
            "| 20261006-C-003 | 棉堆堆袜 | 4600 | 已确认 |",
        ])
        totals, dates = uploader._records(text, 'C')
        self.assertEqual(totals['棉堆堆袜'], 4600)
        self.assertEqual(totals['冰冰袜'], 0)
        self.assertEqual(dates, {'2026-10-06'})
