import json
import sys
import tempfile
import types
import urllib.error
import unittest
from pathlib import Path

import queue_store as q
import recovery_scan


class CardDeliveryTests(unittest.TestCase):
    def _db_with_report(self, root):
        db = str(Path(root) / 'inbox.sqlite')
        q.init(db)
        q.put(db, {'messageId': 'om_report', 'senderId': 'ou_worker',
                   'groupId': recovery_scan.GROUP,
                   'content': '棉堆堆袜2300\n冰冰袜1500\n小腿袜300\n过膝袜0\n女船袜0\n男船袜0'})
        result = {'extracted': {'kind': 'report', 'worker': 'unknown',
                                'production_date': '2026-09-24', 'items': [
                                    {'product': '棉堆堆袜', 'quantity': 2300},
                                ]}}
        with q.conn(db) as c:
            c.execute("UPDATE inbox SET status='ready', result=? WHERE id='om_report'",
                      (json.dumps(result),))
        return db

    def test_failed_delivery_is_retried_and_success_is_idempotent(self):
        with tempfile.TemporaryDirectory() as root:
            db = self._db_with_report(root)
            calls = []
            transport = types.ModuleType('transport')

            def request(*args, **kwargs):
                calls.append(1)
                if len(calls) == 1:
                    raise RuntimeError('temporary network failure')
                return {'data': {'message_id': 'om_card'}}

            transport.request = request
            old = sys.modules.get('transport')
            sys.modules['transport'] = transport
            try:
                recovery_scan.scan(db)
                with q.conn(db) as c:
                    self.assertEqual(c.execute("SELECT delivery FROM review_cards").fetchone()[0], 'failed')
                    c.execute("UPDATE review_cards SET delivery_next_at=?", (recovery_scan.time.time() - 1,))
                recovery_scan.scan(db)
                with q.conn(db) as c:
                    self.assertEqual(tuple(c.execute("SELECT delivery,message_id FROM review_cards").fetchone()), ('sent', 'om_card'))
                    self.assertIsNotNone(c.execute("SELECT sent_at FROM review_cards").fetchone()[0])
                recovery_scan.scan(db)
                self.assertEqual(len(calls), 2)
            finally:
                if old is None:
                    sys.modules.pop('transport', None)
                else:
                    sys.modules['transport'] = old

    def test_sent_but_unconfirmed_card_is_reminded_after_delay(self):
        with tempfile.TemporaryDirectory() as root:
            db = self._db_with_report(root)
            calls = []
            transport = types.ModuleType('transport')

            def request(*args, **kwargs):
                if args and len(args)>1 and args[1] == 'GET':
                    return {'data': {'items': [{'deleted': False}]}}
                calls.append(kwargs.get('data') or args[-1])
                return {'data': {'message_id': f'om_card_{len(calls)}'}}

            transport.request = request
            old = sys.modules.get('transport')
            sys.modules['transport'] = transport
            try:
                recovery_scan.scan(db)
                with q.conn(db) as c:
                    c.execute("UPDATE review_cards SET sent_at=?, last_reminder_at=NULL WHERE delivery='sent'",
                              (recovery_scan.time.time() - recovery_scan.REMINDER_DELAYS[0] - 1,))
                recovery_scan.scan(db)
                self.assertEqual(len(calls), 2)
                with q.conn(db) as c:
                    row = c.execute("SELECT delivery,resend_count,last_reminder_at FROM review_cards").fetchone()
                self.assertEqual(row['delivery'], 'sent')
                self.assertEqual(row['resend_count'], 1)
                self.assertIsNotNone(row['last_reminder_at'])
                recovery_scan.scan(db)
                self.assertEqual(len(calls), 2)
            finally:
                if old is None:
                    sys.modules.pop('transport', None)
                else:
                    sys.modules['transport'] = old

    def test_card_payload_uses_feishu_legacy_text_card_shape(self):
        with tempfile.TemporaryDirectory() as root:
            db = self._db_with_report(root)
            captured = []
            transport = types.ModuleType('transport')

            def request(*args, **kwargs):
                payload = kwargs.get('data') or args[-1]
                captured.append(json.loads(payload['content']))
                return {'data': {'message_id': 'om_card_shape'}}

            transport.request = request
            old = sys.modules.get('transport')
            sys.modules['transport'] = transport
            try:
                recovery_scan.scan(db)
                self.assertEqual(len(captured), 1)
                self.assertEqual(captured[0]['header']['title']['content'], '生产报数核对')
                self.assertEqual(captured[0]['elements'][0]['tag'], 'div')
                self.assertEqual(captured[0]['elements'][0]['text']['tag'], 'lark_md')
            finally:
                if old is None:
                    sys.modules.pop('transport', None)
                else:
                    sys.modules['transport'] = old

    def test_http_400_is_recorded_as_explicit_failure_not_unknown(self):
        with tempfile.TemporaryDirectory() as root:
            db = self._db_with_report(root)
            transport = types.ModuleType('transport')

            def request(*args, **kwargs):
                raise urllib.error.HTTPError('https://feishu.test', 400, 'bad request', {}, None)

            transport.request = request
            old = sys.modules.get('transport')
            sys.modules['transport'] = transport
            try:
                recovery_scan.scan(db)
                with q.conn(db) as c:
                    row = c.execute(
                        'SELECT delivery,delivery_next_at FROM review_cards'
                    ).fetchone()
                self.assertEqual(row['delivery'], 'failed')
                self.assertIsNotNone(row['delivery_next_at'])
            finally:
                if old is None:
                    sys.modules.pop('transport', None)
                else:
                    sys.modules['transport'] = old


if __name__ == '__main__':
    unittest.main()
