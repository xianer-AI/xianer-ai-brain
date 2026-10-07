import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import hermes_extract
import queue_store as queue
import recovery_scan
import service
from readonly_query import is_readonly_production_query


class ReadonlyQueryTests(unittest.TestCase):
    def test_explicit_queries_cannot_become_reports_even_with_product_quantities(self):
        queries = [
            '查一下下机翻袜产量，冰冰袜1000双是不是昨天的？',
            '查询烤边产量：棉堆堆袜5000双有没有入账？',
            '请帮我查看2027年1月烤边产量，冰冰袜1000双',
            '小文，帮我看下2026年12月数量记录：小腿袜300双',
            '查询2027年1月徐超超产能',
        ]
        with tempfile.TemporaryDirectory() as root:
            db = str(Path(root) / 'inbox.sqlite')
            for index, content in enumerate(queries):
                with self.subTest(content=content):
                    event = dict(messageId=f'om_query_{index}', senderId='ou_fixture',
                                 groupId=recovery_scan.GROUP, content=content)
                    self.assertTrue(is_readonly_production_query(content))
                    self.assertIsNone(hermes_extract.deterministic_report(event))
                    self.assertEqual(hermes_extract.extract(event)['extracted']['kind'], 'other')
                    result = service.fast_extract(event)
                    self.assertEqual(result['extracted']['kind'], 'other')
                    self.assertEqual(result['extracted']['items'], [])
                    queue.put(db, event)
                    job = queue.claim(db)
                    with patch.object(service.subprocess, 'run', side_effect=AssertionError('model call forbidden')):
                        self.assertEqual(service.extract_job(job), result)
                    queue.finish(db, job['id'], job['lease'], result)
            with patch.object(recovery_scan, 'deliver'):
                self.assertEqual(recovery_scan.scan(db), [])
            with queue.conn(db) as c:
                self.assertEqual(c.execute('SELECT COUNT(*) FROM review_cards').fetchone()[0], 0)

    def test_existing_reports_backfills_corrections_and_ambiguous_text_keep_their_path(self):
        products = '\n'.join(p + '：1' for p in service.PRODUCTS)
        for prefix in ['', '报数\n', '补报2026-10-06\n', '上报\n', '更正\n', '查询后更正产量\n']:
            content = prefix + 'B=梅芳\n' + products
            with self.subTest(prefix=prefix):
                self.assertFalse(is_readonly_production_query(content))
                event = {'content': content, 'timestamp': '2026-10-07T20:00:00+08:00'}
                self.assertEqual(service.fast_extract(event)['extracted']['kind'], 'report')
                self.assertEqual(hermes_extract.deterministic_report(event)['kind'], 'report')
        self.assertFalse(is_readonly_production_query('冰冰袜1000双是不是昨天的？'))


if __name__ == '__main__':
    unittest.main()
