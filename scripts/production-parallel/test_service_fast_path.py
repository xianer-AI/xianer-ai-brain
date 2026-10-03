import json
import unittest
from unittest.mock import patch

import service


class ServiceFastPathTests(unittest.TestCase):
    def test_standard_report_uses_local_parser_without_model_subprocess(self):
        event = {
            'messageId': 'om_fast_path',
            'senderId': 'ou_worker',
            'groupId': service.recovery_scan.GROUP,
            'content': 'B\n棉堆堆袜：2400\n冰冰袜：1600\n小腿袜：200\n过膝袜：0\n女船袜：0\n男船袜：0',
            'timestamp': '2026-09-28T08:00:00+08:00',
        }
        job = {'event': json.dumps(event, ensure_ascii=False)}
        with patch.object(service.subprocess, 'run', side_effect=AssertionError('不应启动模型子进程')):
            data = service.extract_job(job)
        self.assertEqual(data['extracted']['kind'], 'report')
        self.assertEqual(data['extracted']['worker'], 'B')
        self.assertEqual(len(data['extracted']['items']), 6)

    def test_common_product_typo_is_normalized_to_canonical_name(self):
        event = {
            'messageId': 'om_fast_product_alias',
            'senderId': 'ou_worker',
            'groupId': service.recovery_scan.GROUP,
            'content': 'B\n棉堆堆袜：2400\n冰袜袜：1800\n小腿袜：300\n过膝袜：0\n女船袜：0\n男船袜：0',
            'timestamp': '2026-10-01T10:00:00+08:00',
        }
        data = service.fast_extract(event)
        extracted = data['extracted']
        self.assertEqual(extracted['kind'], 'report')
        self.assertEqual(len(extracted['items']), 6)
        self.assertEqual(
            next(item['quantity'] for item in extracted['items'] if item['product'] == '冰冰袜'),
            1800,
        )
        self.assertEqual(extracted['missing'], [])
        self.assertIn('冰袜袜→冰冰袜', '；'.join(extracted['notes']))

    def test_standard_report_without_date_uses_shanghai_message_date(self):
        event = {
            'messageId': 'om_fast_date',
            'senderId': 'ou_worker',
            'groupId': service.recovery_scan.GROUP,
            'content': 'B\n棉堆堆袜：2400\n冰冰袜：1600\n小腿袜：200\n过膝袜：0\n女船袜：0',
            'timestamp': '2026-09-28T10:00:00+08:00',
        }
        data = service.fast_extract(event)
        self.assertEqual(data['extracted']['production_date'], '2026-09-28')

    def test_standard_report_respects_makeup_yesterday_cutoff(self):
        event = {
            'messageId': 'om_fast_makeup_date',
            'senderId': 'ou_worker',
            'groupId': service.recovery_scan.GROUP,
            'content': 'B\n补昨天\n棉堆堆袜：2400\n冰冰袜：1600\n小腿袜：200\n过膝袜：0\n女船袜：0',
            'timestamp': '2026-09-28T10:00:00+08:00',
        }
        data = service.fast_extract(event)
        self.assertEqual(data['extracted']['production_date'], '2026-09-27')

    def test_historical_backfill_text_keeps_explicit_date_for_normal_review(self):
        """A manual backfill is still a normal report pending employee confirmation."""
        event = {
            'messageId': 'om_fast_backfill_date',
            'senderId': 'ou_worker',
            'groupId': service.recovery_scan.GROUP,
            'content': '补报日期：2026-09-20\nB=梅芳\n棉堆堆袜：2400\n冰冰袜：1600\n小腿袜：200\n过膝袜：0\n女船袜：0\n男船袜：0',
            'timestamp': '2026-10-01T10:00:00+08:00',
        }
        data = service.fast_extract(event)
        self.assertEqual(data['extracted']['kind'], 'report')
        self.assertEqual(data['extracted']['production_date'], '2026-09-20')
        self.assertEqual(len(data['extracted']['items']), 6)
        self.assertTrue(data['extracted']['backfill'])

    def test_normal_report_is_not_marked_as_backfill(self):
        event = {
            'messageId': 'om_fast_normal_marker',
            'senderId': 'ou_worker',
            'groupId': service.recovery_scan.GROUP,
            'content': 'B\n生产日期：2026-09-20\n棉堆堆袜：2400\n冰冰袜：1600\n小腿袜：200\n过膝袜：0\n女船袜：0\n男船袜：0',
            'timestamp': '2026-09-20T10:00:00+08:00',
        }
        data = service.fast_extract(event)
        self.assertFalse(data['extracted']['backfill'])

    def test_date_prefixed_backfill_is_marked(self):
        event = {
            'messageId': 'om_fast_backfill_prefix',
            'senderId': 'ou_worker',
            'groupId': service.recovery_scan.GROUP,
            'content': '补2026-09-20\nB=梅芳\n棉堆堆袜：1\n冰冰袜：2\n小腿袜：3\n过膝袜：0\n女船袜：0\n男船袜：0',
            'timestamp': '2026-10-01T10:00:00+08:00',
        }
        data = service.fast_extract(event)
        self.assertTrue(data['extracted']['backfill'])


if __name__ == '__main__':
    unittest.main()
