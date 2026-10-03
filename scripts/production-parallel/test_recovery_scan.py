import tempfile
import unittest
from pathlib import Path

import queue_store as q
import recovery_scan


class RecoveryScanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / 'inbox.sqlite')
        q.put(self.db, {
            'messageId': 'om_source_1', 'senderId': 'sender-a',
            'groupId': recovery_scan.GROUP,
            'content': '冰冰袜：100双 棉堆堆袜：200双 小腿袜：0双 过膝袜：0双 女船袜：0双 男船袜：0双',
            'timestamp': '2026-09-23T12:00:00+08:00',
        })
        q.finish(self.db, 'om_source_1', q.claim(self.db)['lease'], {
            'agent': '规则化报数解析', 'draft_only': True,
            'extracted': {'kind': 'report', 'worker': 'unknown',
                          'items': [{'product': '过膝袜', 'quantity': 0, 'process': '下机'}],
                          'missing': [], 'production_date': '2026-09-23'}
        })

    def tearDown(self):
        self.tmp.cleanup()

    def test_recovery_scan_creates_one_pending_card_and_is_idempotent(self):
        first = recovery_scan.scan(self.db)
        second = recovery_scan.scan(self.db)
        self.assertEqual(first, ['om_source_1'])
        self.assertEqual(second, [])
        with q.conn(self.db) as c:
            row = c.execute("SELECT state, summary FROM review_cards WHERE source='om_source_1'").fetchone()
        self.assertEqual(row['state'], 'pending')
        self.assertIn('网络恢复后补处理', row['summary'])
        self.assertIn('棉堆堆袜：0 双（数量为0，请核实）', row['summary'])

    def test_existing_confirmed_or_uploaded_source_is_skipped(self):
        recovery_scan.scan(self.db)
        with q.conn(self.db) as c:
            c.execute("UPDATE review_cards SET state='confirmed' WHERE source='om_source_1'")
        self.assertEqual(recovery_scan.scan(self.db), [])

    def test_superseded_source_is_not_recreated_after_manual_archive(self):
        recovery_scan.scan(self.db)
        with q.conn(self.db) as c:
            c.execute("UPDATE review_cards SET state='superseded' WHERE source=?", ('om_source_1',))
        self.assertEqual(recovery_scan.scan(self.db), [])

    def test_empty_report_draft_does_not_create_all_unknown_card(self):
        q.put(self.db, {
            'messageId': 'om_empty', 'senderId': 'sender-a',
            'groupId': recovery_scan.GROUP,
            'content': '未识别的测试内容',
        })
        job = q.claim(self.db)
        q.finish(self.db, 'om_empty', job['lease'], {
            'extracted': {'kind': 'report', 'worker': 'unknown',
                          'items': [], 'missing': [], 'production_date': '2026-09-23'}
        })
        self.assertEqual(recovery_scan.scan(self.db), ['om_source_1'])
        with q.conn(self.db) as c:
            self.assertIsNone(c.execute("SELECT 1 FROM review_cards WHERE source='om_empty'").fetchone())

    def test_suppressed_historical_source_is_not_recreated(self):
        recovery_scan.suppress_source(self.db, 'om_source_1', 'manual historical cleanup')
        self.assertEqual(recovery_scan.scan(self.db), [])

    def test_verified_upload_source_is_suppressed_and_not_recreated(self):
        import hashlib, json
        receipt_dir = Path(self.db).parent / 'receipts'
        receipt_dir.mkdir()
        (receipt_dir / (hashlib.sha256(b'om_source_1').hexdigest() + '.json')).write_text(json.dumps({
            'source': 'om_source_1', 'confirmation': 'om_confirmed',
            'commit': 'abc123', 'status': 'verified'}))
        self.assertEqual(recovery_scan.scan(self.db), [])
        with q.conn(self.db) as c:
            self.assertIsNone(c.execute("SELECT 1 FROM review_cards WHERE source='om_source_1'").fetchone())

    def test_temporary_test_database_cannot_send_to_feishu(self):
        self.assertFalse(recovery_scan.delivery_allowed(self.db))


if __name__ == '__main__':
    unittest.main()
