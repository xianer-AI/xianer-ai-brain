import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import queue_store as q
import review_cards

from batch_status import get_batch_status, latest_actionable_batch


PRODUCTS = ('棉堆堆袜', '冰冰袜', '小腿袜', '过膝袜', '女船袜', '男船袜')


def _report(db, source, sender='ou_a', date='2026-09-26'):
    q.put(db, {
        'messageId': source,
        'senderId': sender,
        'groupId': review_cards.GROUP,
        'content': '\n'.join(f'{product}0' for product in PRODUCTS),
    })
    result = {'extracted': {
        'kind': 'report', 'worker': 'A', 'production_date': date,
        'items': [{'product': product, 'process': '下机', 'quantity': 0}
                  for product in PRODUCTS],
    }}
    with q.conn(db) as conn:
        conn.execute('UPDATE inbox SET status="ready", result=? WHERE id=?',
                     (json.dumps(result, ensure_ascii=False), source))


def _card(db, source, token, sender='ou_a', state='pending', delivery='sent'):
    now = review_cards.time.time()
    with q.conn(db) as conn:
        conn.execute(
            '''INSERT INTO review_cards
               (token,source,sender,grp,summary,digest,expires,state,callback,result,delivery)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
            (token, source, sender, review_cards.GROUP, '合计：0', 'digest',
             now + 86400, state, None, None, delivery))


def _verified_receipt(db, source, confirmation='om_confirm'):
    receipt_dir = Path(db).parent / 'receipts'
    receipt_dir.mkdir()
    path = receipt_dir / (hashlib.sha256(source.encode()).hexdigest() + '.json')
    path.write_text(json.dumps({
        'source': source, 'confirmation': confirmation,
        'commit': 'abc123', 'status': 'verified',
    }))


def _confirmation_receipt(db, source, status='pending_dispatch', confirmation='om_confirm', error=None):
    with q.conn(db) as conn:
        conn.execute(
            '''INSERT INTO confirmation_receipts
               (message_id,sender,grp,text,source,token,status,error,attempts,next_at,created)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
            (confirmation, 'ou_a', review_cards.GROUP, '准确', source,
             'tok_0926', status, error, 3, 0, 0))


class BatchStatusTests(unittest.TestCase):
    def test_status_exposes_version_and_sync_protocol(self):
        with tempfile.TemporaryDirectory() as directory:
            db = str(Path(directory) / 'inbox.sqlite')
            q.init(db)
            status = get_batch_status(db, 'om_missing')
            self.assertEqual(status['workbench_version'], 'V1.17')
            self.assertEqual(status['sync_protocol_version'], 'S1')
            self.assertEqual(status['card_protocol_version'], 'CARD-INTERACTIVE-1')

    def test_verified_historical_batch_is_completed_not_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            db = str(Path(directory) / 'inbox.sqlite')
            q.init(db)
            review_cards.init(db)
            _report(db, 'om_0926')
            _card(db, 'om_0926', 'tok_0926', state='confirmed')
            _verified_receipt(db, 'om_0926')

            status = get_batch_status(db, 'om_0926')

            self.assertEqual(status['state'], 'completed')
            self.assertEqual(status['upload_state'], 'verified')
            self.assertNotEqual(status['state'], 'awaiting_confirmation')

    def test_latest_actionable_batch_skips_verified_history(self):
        with tempfile.TemporaryDirectory() as directory:
            db = str(Path(directory) / 'inbox.sqlite')
            q.init(db)
            review_cards.init(db)
            _report(db, 'om_0926')
            _card(db, 'om_0926', 'tok_0926', state='confirmed')
            _verified_receipt(db, 'om_0926')
            _report(db, 'om_0927', date='2026-09-27')
            _card(db, 'om_0927', 'tok_0927', state='pending')

            status = latest_actionable_batch(db, 'ou_a', review_cards.GROUP)

            self.assertEqual(status['source'], 'om_0927')
            self.assertEqual(status['state'], 'awaiting_confirmation')

    def test_unknown_delivery_requires_reconciliation(self):
        with tempfile.TemporaryDirectory() as directory:
            db = str(Path(directory) / 'inbox.sqlite')
            q.init(db)
            review_cards.init(db)
            _report(db, 'om_unknown')
            _card(db, 'om_unknown', 'tok_unknown', delivery='unknown')

            status = get_batch_status(db, 'om_unknown')

            self.assertEqual(status['state'], 'needs_reconciliation')
            self.assertEqual(status['delivery_state'], 'unknown')

    def test_blocked_confirmation_is_upload_failed_without_reconfirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            db = str(Path(directory) / 'inbox.sqlite')
            q.init(db)
            review_cards.init(db)
            _report(db, 'om_blocked')
            _card(db, 'om_blocked', 'tok_blocked', state='confirmed')
            _confirmation_receipt(db, 'om_blocked', status='blocked')

            status = get_batch_status(db, 'om_blocked')

            self.assertEqual(status['state'], 'upload_failed')
            self.assertEqual(status['upload_state'], 'failed')
            self.assertNotIn('重新', status['reason'])
            self.assertNotIn('重报', status['reason'])

    def test_blocked_ledger_upload_is_explicit_and_never_requests_reconfirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            db = str(Path(directory) / 'inbox.sqlite')
            q.init(db)
            review_cards.init(db)
            _report(db, 'om_ledger_failed', date='2026-10-01')
            _card(db, 'om_ledger_failed', 'tok_ledger_failed', state='confirmed')
            _confirmation_receipt(
                db, 'om_ledger_failed', status='blocked', confirmation='om_ledger_confirm',
                error='RuntimeError: 远程台账缺少个人累计区',
            )

            status = get_batch_status(db, 'om_ledger_failed')

            self.assertEqual(status['state'], 'upload_failed')
            self.assertEqual(status['upload_state'], 'failed')
            self.assertEqual(status['failure_kind'], 'ledger_structure')
            self.assertEqual(status['reason'], '已确认，上传因台账月份结构失败')
            self.assertNotIn('重新', status['reason'])
            self.assertNotIn('重报', status['reason'])

    def test_latest_actionable_batch_keeps_retryable_upload_visible(self):
        with tempfile.TemporaryDirectory() as directory:
            db = str(Path(directory) / 'inbox.sqlite')
            q.init(db)
            review_cards.init(db)
            _report(db, 'om_latest_failed', date='2026-10-01')
            _card(db, 'om_latest_failed', 'tok_latest_failed', state='confirmed')
            _confirmation_receipt(
                db, 'om_latest_failed', status='blocked', confirmation='om_latest_confirm',
                error='上传任务失败（code=1）：远程台账缺少个人累计区',
            )

            status = latest_actionable_batch(db, 'ou_a', review_cards.GROUP)

            self.assertEqual(status['state'], 'confirmed')
            self.assertEqual(status['upload_state'], 'queued')

    def test_verified_github_waits_for_success_reply_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            db = str(Path(directory) / 'inbox.sqlite')
            q.init(db)
            review_cards.init(db)
            _report(db, 'om_reply_pending')
            _card(db, 'om_reply_pending', 'tok_reply', state='confirmed')
            _verified_receipt(db, 'om_reply_pending')
            _confirmation_receipt(db, 'om_reply_pending', status='pending_dispatch')

            status = get_batch_status(db, 'om_reply_pending')

            self.assertEqual(status['state'], 'reply_pending')
            self.assertEqual(status['upload_state'], 'verified_reply_pending')

            with q.conn(db) as conn:
                conn.execute("UPDATE confirmation_receipts SET status='dispatched' WHERE source=?",
                             ('om_reply_pending',))
            status = get_batch_status(db, 'om_reply_pending')
            self.assertEqual(status['state'], 'completed')


if __name__ == '__main__':
    unittest.main()
