import unittest
from commit_guard import format_success_receipt, report_from_inbox_row


class ReceiptFormatTests(unittest.TestCase):
    def test_success_receipt_contains_details_and_three_credentials(self):
        receipt = format_success_receipt({
            'worker': 'C', 'production_date': '2026-09-23', 'process': '烤边',
            'items': [
                {'product': '棉堆堆袜', 'quantity': 2400},
                {'product': '冰冰袜', 'quantity': 1800},
                {'product': '小腿袜', 'quantity': 500},
                {'product': '过膝袜', 'quantity': 0},
                {'product': '女船袜', 'quantity': 0},
                {'product': '男船袜', 'quantity': 0},
            ], 'total': 4700,
        }, 'om_source', 'om_confirmation', 'abc123', '来源消息、确认消息、明细和合计均已写入远程台账。')
        self.assertIn('已确认并上传成功。', receipt)
        self.assertIn('• 员工：C（李鸿玉）\n', receipt)
        self.assertIn('棉堆堆袜：2400 双', receipt)
        self.assertIn('男船袜：0 双', receipt)
        self.assertIn('原始报数消息：om_source', receipt)
        self.assertIn('确认消息：om_confirmation', receipt)
        self.assertIn('GitHub commit：abc123', receipt)
        self.assertIn('已回读 GitHub 核实', receipt)
        self.assertIn('GitHub 回执', receipt)
        self.assertIn('已更新：2026下半年下机白胚半成品统计.md', receipt)
        self.assertIn('同步验收：全部一致', receipt)
        self.assertIn('生产统计工作台版本：V1.17', receipt)
        self.assertIn('远程 commit：abc123', receipt)

    def test_missing_commit_cannot_be_presented_as_success(self):
        with self.assertRaises(ValueError):
            format_success_receipt({'items': []}, 'om_source', 'om_confirmation', '', '未完成')

    def test_success_receipt_uses_zero_for_unreported_products(self):
        receipt = format_success_receipt({
            'worker': 'B', 'production_date': '2026-09-28', 'process': '下机',
            'items': [{'product': '棉堆堆袜', 'quantity': 2500}], 'total': 2500,
        }, 'om_source', 'om_confirmation', 'abc123', '已回读')
        self.assertIn('• 员工：B（梅芳）\n', receipt)
        self.assertIn('过膝袜：0 双', receipt)
        self.assertNotIn('过膝袜：未上报 双', receipt)

    def test_backfill_receipt_marks_type_and_bold_date(self):
        receipt = format_success_receipt({
            'worker': 'B', 'production_date': '2026-09-20', 'process': '下机',
            'backfill': True,
            'items': [{'product': product, 'quantity': 0} for product in (
                '棉堆堆袜', '冰冰袜', '小腿袜', '过膝袜', '女船袜', '男船袜')],
            'total': 0,
        }, 'om_source', 'om_confirmation', 'abc123', '已回读')
        self.assertIn('• 生产日：**2026-09-20**', receipt)
        self.assertIn('• 记录类型：**补报**', receipt)

    def test_report_is_built_from_inbox_row_dict(self):
        row = {'result': {'extracted': {'worker': 'C', 'production_date': '2026-09-23', 'items': [
            {'product': '棉堆堆袜', 'process': '烤边', 'quantity': 10},
        ]}}}
        report = report_from_inbox_row(row)
        self.assertEqual(report['worker'], 'C')
        self.assertEqual(report['items'][0]['quantity'], 10)
        self.assertEqual(report['total'], 10)

    def test_unreported_products_are_zero_once_in_receipt_report(self):
        row = {'result': {'extracted': {'worker': 'B', 'production_date': '2026-09-28', 'items': [
            {'product': '棉堆堆袜', 'process': '下机', 'quantity': 2500},
        ]}}}
        report = report_from_inbox_row(row)
        quantities = {item['product']: item['quantity'] for item in report['items']}
        self.assertEqual(len(report['items']), 6)
        self.assertEqual(quantities['过膝袜'], 0)
        self.assertEqual(report['total'], 2500)


if __name__ == '__main__':
    unittest.main()
