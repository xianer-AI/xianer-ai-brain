import json
import tempfile
import types
import urllib.error
import unittest
from pathlib import Path
from unittest.mock import patch

import queue_store as q
import review_cards


class ReviewCardRetryTests(unittest.TestCase):
    def test_init_migrates_active_legacy_missing_text_to_zero_review_text(self):
        with tempfile.TemporaryDirectory() as d:
            db = str(Path(d) / 'inbox.sqlite')
            q.init(db)
            review_cards.init(db)
            now = review_cards.time.time()
            with q.conn(db) as c:
                c.execute("""INSERT INTO review_cards
                    (token,source,sender,grp,summary,digest,expires,state,callback,result)
                    VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    ('tok_legacy', 'om_legacy', 'ou_a', review_cards.GROUP,
                     '过膝袜：核实\n男船袜：未上报\n合计：1', 'digest', now + 86400,
                     'pending', None, None))
            review_cards.init(db)
            with q.conn(db) as c:
                summary = c.execute('SELECT summary FROM review_cards WHERE token=?', ('tok_legacy',)).fetchone()[0]
            self.assertIn('过膝袜：0双（数量为0，请核实）', summary)
            self.assertIn('男船袜：0双（数量为0，请核实）', summary)

    def test_unreported_product_is_zero_reviewed_and_confirmation_is_dispatchable(self):
        with tempfile.TemporaryDirectory() as d:
            db = str(Path(d) / 'inbox.sqlite')
            q.init(db)
            source = 'om_missing_knee'
            q.put(db, {
                'messageId': source, 'senderId': 'ou_a',
                'groupId': review_cards.GROUP,
                'content': 'B\n棉堆堆袜：2400\n冰冰袜：1600\n小腿袜：200\n女船袜：0\n男船袜：0',
            })
            review_cards.init(db)
            with q.conn(db) as c:
                result = {'extracted': {'kind': 'report', 'worker': 'B',
                    'production_date': '2026-09-28', 'items': [
                        {'product': p, 'process': '下机', 'quantity': qv}
                        for p, qv in [('棉堆堆袜', 2400), ('冰冰袜', 1600),
                                      ('小腿袜', 200), ('女船袜', 0), ('男船袜', 0)]],
                    'missing': ['过膝袜']}}
                c.execute("UPDATE inbox SET status='ready', result=? WHERE id=?",
                          (json.dumps(result, ensure_ascii=False), source))
                c.execute('CREATE TABLE worker_identity(worker TEXT,name TEXT,platform TEXT PRIMARY KEY,owner TEXT,proof TEXT,created REAL)')
                c.execute("INSERT INTO worker_identity VALUES('B','梅芳','ou_a','test',?,0)", (source,))
            card = review_cards.issue(
                db, source,
                '生产日：2026-09-28\n工序：下机\n'
                '棉堆堆袜：2400\n冰冰袜：1600\n小腿袜：200\n'
                '过膝袜：核实\n女船袜：0\n男船袜：0\n合计：4200'
            )
            with q.conn(db) as c:
                summary = c.execute('SELECT summary FROM review_cards WHERE token=?', (card['token'],)).fetchone()[0]
            self.assertIn('过膝袜：0双（数量为0，请核实）', summary)
            result = review_cards.act_text(db, '准确', 'ou_a', review_cards.GROUP, 'om_confirm_missing_knee')
            self.assertEqual(result['source'], source)
            with q.conn(db) as c:
                status = c.execute(
                    "SELECT status FROM confirmation_receipts WHERE message_id=?",
                    ('om_confirm_missing_knee',),
                ).fetchone()[0]
            self.assertEqual(status, 'pending_dispatch')

    def test_failed_card_delivery_enters_30_second_retry_queue(self):
        with tempfile.TemporaryDirectory() as d:
            db = str(Path(d) / 'inbox.sqlite')
            q.init(db)
            review_cards.init(db)
            now = review_cards.time.time()
            with q.conn(db) as c:
                c.execute("""INSERT INTO review_cards
                    (token,source,sender,grp,summary,digest,expires,state,callback,result)
                    VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    ('tok_failed', 'om_source', 'ou_a', review_cards.GROUP,
                     '身份：A=徐超超\n合计：1', 'digest', now + 86400,
                     'pending', None, None))
            review_cards.mark_card_delivery_failed(db, 'tok_failed', 'timeout', now=now)
            cards = review_cards.pending_card_deliveries(db, now=now + 31)
            self.assertEqual([row['token'] for row in cards], ['tok_failed'])
            self.assertEqual(cards[0]['delivery_error'], 'timeout')

    def test_deliver_card_http_400_is_explicit_failure_not_unknown(self):
        with tempfile.TemporaryDirectory() as d:
            db = str(Path(d) / 'inbox.sqlite')
            q.init(db)
            review_cards.init(db)
            now = review_cards.time.time()
            with q.conn(db) as c:
                c.execute("""INSERT INTO review_cards
                    (token,source,sender,grp,summary,digest,expires,state,callback,result)
                    VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    ('tok_http_400', 'om_source', 'ou_a', review_cards.GROUP,
                     '身份：A=徐超超\n合计：1', 'digest', now + 86400,
                     'pending', None, None))
            transport = types.ModuleType('transport')

            def request(*args, **kwargs):
                raise urllib.error.HTTPError('https://feishu.test', 400, 'bad request', {}, None)

            transport.request = request
            old = __import__('sys').modules.get('transport')
            __import__('sys').modules['transport'] = transport
            try:
                result = review_cards.deliver_card(db, 'tok_http_400')
                self.assertEqual(result['status'], 'failed')
                with q.conn(db) as c:
                    delivery = c.execute(
                        'SELECT delivery FROM review_cards WHERE token=?',
                        ('tok_http_400',),
                    ).fetchone()[0]
                self.assertEqual(delivery, 'failed')
            finally:
                if old is None:
                    __import__('sys').modules.pop('transport', None)
                else:
                    __import__('sys').modules['transport'] = old

    def test_unconfirmed_card_resends_at_five_then_thirty_minutes_and_stops_after_confirmation(self):
        with tempfile.TemporaryDirectory() as d:
            db = str(Path(d) / 'inbox.sqlite')
            q.init(db)
            review_cards.init(db)
            now = review_cards.time.time()
            with q.conn(db) as c:
                c.execute("""INSERT INTO review_cards
                    (token,source,sender,grp,summary,digest,expires,state,callback,result)
                    VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    ('tok_sent', 'om_source', 'ou_a', review_cards.GROUP,
                     '身份：A=徐超超\n合计：1', 'digest', now + 86400,
                     'pending', None, None))
            review_cards.mark_card_delivery_sent(db, 'tok_sent', 'om_card_1', now=now)
            first = review_cards.pending_card_resends(db, now=now + 301)
            self.assertEqual([row['token'] for row in first], ['tok_sent'])
            review_cards.mark_card_delivery_sent(db, 'tok_sent', 'om_card_2', now=now + 301, is_resend=True)
            self.assertEqual(review_cards.pending_card_resends(db, now=now + 301 + 1801)[0]['resend_count'], 1)
            review_cards.mark_card_delivery_sent(db, 'tok_sent', 'om_card_3', now=now + 301 + 1801, is_resend=True)
            self.assertEqual(review_cards.pending_card_resends(db, now=now + 86400), [])
            with q.conn(db) as c:
                c.execute("UPDATE review_cards SET state='confirmed' WHERE token='tok_sent'")
            self.assertEqual(review_cards.pending_card_resends(db, now=now + 86400), [])

    def test_issue_writes_card_when_delivery_columns_exist(self):
        with tempfile.TemporaryDirectory() as d:
            db = str(Path(d) / 'inbox.sqlite')
            q.init(db)
            review_cards.init(db)
            q.put(db, {
                'messageId': 'om_issue', 'senderId': 'ou_a',
                'groupId': review_cards.GROUP,
                'content': '棉堆堆袜0\n冰冰袜0\n小腿袜0\n过膝袜0\n女船袜0\n男船袜0',
            })
            review_cards.init(db)
            with q.conn(db) as c:
                result = {'extracted': {'kind': 'report', 'worker': 'A',
                    'production_date': '2026-09-24', 'items': [
                        {'product': p, 'process': '下机', 'quantity': 0}
                        for p in ('棉堆堆袜', '冰冰袜', '小腿袜', '过膝袜', '女船袜', '男船袜')
                    ]}}
                c.execute("UPDATE inbox SET status='ready', result=? WHERE id='om_issue'",
                          (json.dumps(result),))
                c.execute('CREATE TABLE worker_identity(worker TEXT,name TEXT,platform TEXT PRIMARY KEY,owner TEXT,proof TEXT,created REAL)')
                c.execute("INSERT INTO worker_identity VALUES('A','徐超超','ou_a','test','om_issue',0)")
            review_cards.init(db)
            summary = '棉堆堆袜：0\n冰冰袜：0\n小腿袜：0\n过膝袜：0\n女船袜：0\n男船袜：0\n合计：0'
            result = review_cards.issue(db, 'om_issue', summary)
            self.assertEqual(result['source'], 'om_issue')
            with q.conn(db) as c:
                row = c.execute('SELECT state,delivery FROM review_cards').fetchone()
            self.assertEqual(tuple(row), ('pending', 'pending'))

    def test_issue_refuses_source_with_verified_upload_receipt(self):
        with tempfile.TemporaryDirectory() as d:
            db = str(Path(d) / 'inbox.sqlite')
            q.init(db)
            q.put(db, {
                'messageId': 'om_uploaded', 'senderId': 'ou_a',
                'groupId': review_cards.GROUP,
                'content': '棉堆堆袜1 冰冰袜0 小腿袜0 过膝袜0 女船袜0 男船袜0',
            })
            review_cards.init(db)
            with q.conn(db) as c:
                result = {'extracted': {'kind': 'report', 'worker': 'A',
                    'production_date': '2026-09-25', 'items': [
                        {'product': p, 'process': '下机', 'quantity': 0}
                        for p in ('棉堆堆袜','冰冰袜','小腿袜','过膝袜','女船袜','男船袜')
                    ]}}
                c.execute("UPDATE inbox SET status='ready', result=? WHERE id='om_uploaded'",
                          (json.dumps(result),))
                c.execute('CREATE TABLE worker_identity(worker TEXT,name TEXT,platform TEXT PRIMARY KEY,owner TEXT,proof TEXT,created REAL)')
                c.execute("INSERT INTO worker_identity VALUES('A','徐超超','ou_a','test','om_uploaded',0)")
            receipt_dir = Path(d) / 'receipts'
            receipt_dir.mkdir()
            import hashlib
            (receipt_dir / (hashlib.sha256(b'om_uploaded').hexdigest() + '.json')).write_text(json.dumps({
                'source': 'om_uploaded', 'confirmation': 'om_confirmed',
                'commit': 'abc123', 'status': 'verified'}))
            summary = '棉堆堆袜：0\n冰冰袜：0\n小腿袜：0\n过膝袜：0\n女船袜：0\n男船袜：0\n合计：0'
            with self.assertRaisesRegex(ValueError, '已上传'):
                review_cards.issue(db, 'om_uploaded', summary)

    def test_confirmation_ignores_pending_card_for_verified_source(self):
        with tempfile.TemporaryDirectory() as d:
            db = str(Path(d) / 'inbox.sqlite')
            q.init(db)
            for source, content in [
                ('om_new_pending', '棉堆堆袜2 冰冰袜0 小腿袜0 过膝袜0 女船袜0 男船袜0'),
                ('om_old_uploaded', '棉堆堆袜1 冰冰袜0 小腿袜0 过膝袜0 女船袜0 男船袜0')]:
                q.put(db, {'messageId': source, 'senderId': 'ou_a',
                           'groupId': review_cards.GROUP, 'content': content})
            review_cards.init(db)
            with q.conn(db) as c:
                for source, qty, date in [('om_new_pending', 2, '2026-09-26'), ('om_old_uploaded', 1, '2026-09-25')]:
                    result = {'extracted': {'kind': 'report', 'worker': 'A',
                        'production_date': date, 'items': [
                            {'product': '棉堆堆袜', 'process': '下机', 'quantity': qty}]}}
                    c.execute('UPDATE inbox SET status="ready", result=? WHERE id=?', (json.dumps(result), source))
                c.execute('CREATE TABLE worker_identity(worker TEXT,name TEXT,platform TEXT PRIMARY KEY,owner TEXT,proof TEXT,created REAL)')
                c.execute("INSERT INTO worker_identity VALUES('A','徐超超','ou_a','test','om_new_pending',0)")
                for token, source in [('oldtoken123456', 'om_old_uploaded'), ('newtoken123456', 'om_new_pending')]:
                    digest = c.execute('SELECT digest FROM inbox WHERE id=?', (source,)).fetchone()[0]
                    c.execute("INSERT INTO review_cards(token,source,sender,grp,summary,digest,expires,state,callback,result) VALUES(?,?,?,?,?,?,?,?,?,?)",
                              (token, source, 'ou_a', review_cards.GROUP, '棉堆堆袜：1\n合计：1', digest, 9999999999, 'pending', None, None))
            receipt_dir = Path(d) / 'receipts'
            receipt_dir.mkdir()
            import hashlib
            (receipt_dir / (hashlib.sha256(b'om_old_uploaded').hexdigest() + '.json')).write_text(json.dumps({
                'source': 'om_old_uploaded', 'confirmation': 'om_old_confirm',
                'commit': 'abc123', 'status': 'verified'}))
            result = review_cards.act_text(db, '准确', 'ou_a', review_cards.GROUP, 'om_new_confirm')
            self.assertEqual(result['source'], 'om_new_pending')

    def test_confirmed_card_can_retry_using_same_source(self):
        with tempfile.TemporaryDirectory() as d:
            db = str(Path(d) / 'inbox.sqlite')
            q.init(db)
            q.put(db, {
                'messageId': 'om_source', 'senderId': 'ou_a',
                'groupId': review_cards.GROUP,
                'content': '棉堆堆袜2300\n冰冰袜1500\n小腿袜300\n过膝袜0\n女船袜0\n男船袜0',
            })
            with q.conn(db) as c:
                result = {'extracted': {'kind': 'report', 'worker': 'A',
                    'production_date': '2026-09-24', 'items': []}}
                c.execute("UPDATE inbox SET status='ready', result=? WHERE id='om_source'",
                          (json.dumps(result),))
                digest = c.execute("SELECT digest FROM inbox WHERE id='om_source'").fetchone()[0]
                c.execute('CREATE TABLE worker_identity(worker TEXT,name TEXT,platform TEXT PRIMARY KEY,owner TEXT,proof TEXT,created REAL)')
                c.execute("INSERT INTO worker_identity VALUES('A','徐超超','ou_a','test','om_source',0)")
            review_cards.init(db)
            with q.conn(db) as c:
                c.execute("INSERT INTO review_cards(token,source,sender,grp,summary,digest,expires,state,callback,result) VALUES(?,?,?,?,?,?,?,?,?,?)",
                          ('abcdef123456', 'om_source', 'ou_a', review_cards.GROUP,
                           '身份：A=徐超超\n合计：4100', digest, 9999999999,
                           'confirmed', 'om_conf1', json.dumps({'source': 'om_source'})))
            result = review_cards.act_text(db, '准确', 'ou_a', review_cards.GROUP, 'om_conf2')
            self.assertEqual(result['source'], 'om_source')
            self.assertTrue(result['retrigger'])
            with q.conn(db) as c:
                receipt = c.execute("SELECT source,token,status FROM confirmation_receipts WHERE message_id='om_conf2'").fetchone()
            self.assertEqual(tuple(receipt), ('om_source', 'abcdef123456', 'pending_dispatch'))
            self.assertTrue(review_cards.claim_confirmation_dispatch(db, 'om_conf2'))
            self.assertFalse(review_cards.claim_confirmation_dispatch(db, 'om_conf2'))
            with q.conn(db) as c:
                self.assertEqual(c.execute("SELECT status FROM confirmation_receipts WHERE message_id='om_conf2'").fetchone()[0], 'dispatching')
            import hashlib
            receipts = Path(d) / 'receipts'
            receipts.mkdir()
            (receipts / (hashlib.sha256(b'om_source').hexdigest() + '.json')).write_text(json.dumps({
                'source': 'om_source', 'confirmation': 'om_conf2', 'commit': 'abc123', 'status': 'verified'}))
            review_cards.mark_confirmation_dispatched(db, 'om_conf2')
            self.assertEqual(review_cards.pending_confirmations(db), [])

    def test_failed_confirmation_is_durable_and_replayed_idempotently(self):
        with tempfile.TemporaryDirectory() as d:
            db = str(Path(d) / 'inbox.sqlite')
            q.init(db)
            q.put(db, {
                'messageId': 'om_source', 'senderId': 'ou_a',
                'groupId': review_cards.GROUP,
                'content': '棉堆堆袜1\n冰冰袜2\n小腿袜3\n过膝袜0\n女船袜0\n男船袜0',
            })
            review_cards.init(db)
            with q.conn(db) as c:
                result = {'extracted': {'kind': 'report', 'worker': 'A',
                    'production_date': '2026-09-24', 'items': [
                        {'product': '棉堆堆袜', 'quantity': 1}]}}
                c.execute("UPDATE inbox SET status='ready', result=? WHERE id='om_source'",
                          (json.dumps(result),))
                digest = c.execute("SELECT digest FROM inbox WHERE id='om_source'").fetchone()[0]
                c.execute('CREATE TABLE worker_identity(worker TEXT,name TEXT,platform TEXT PRIMARY KEY,owner TEXT,proof TEXT,created REAL)')
                c.execute("INSERT INTO worker_identity VALUES('A','徐超超','ou_a','test','om_source',0)")
                c.execute("INSERT INTO review_cards(token,source,sender,grp,summary,digest,expires,state,callback,result) VALUES(?,?,?,?,?,?,?,?,?,?)",
                          ('abcdef123456', 'om_source', 'ou_a', review_cards.GROUP,
                           '身份：A=徐超超\n合计：1', digest, 9999999999,
                           'confirmed', 'om_conf1', json.dumps({'source': 'om_source'})))
                c.execute("UPDATE review_cards SET confirmation_message_id='om_conf1',confirmation_retry_status='bound' WHERE token='abcdef123456'")
            review_cards.record_failed_confirmation(db, '准确', 'ou_a', review_cards.GROUP,
                                                    'om_conf1', 'temporary failure')
            pending = review_cards.pending_confirmations(db, review_cards.time.time() + 31)
            self.assertEqual(len(pending), 1)
            result = review_cards.replay_confirmation(db, pending[0])
            self.assertEqual(result['source'], 'om_source')
            with q.conn(db) as c:
                row = c.execute("SELECT state,confirmation_message_id,confirmation_retry_status FROM review_cards").fetchone()
            self.assertEqual(row['state'], 'confirmed')
            self.assertEqual(row['confirmation_message_id'], 'om_conf1')
            self.assertEqual(row['confirmation_retry_status'], 'bound')
            with q.conn(db) as c:
                receipt = c.execute("SELECT source,token,status FROM confirmation_receipts WHERE message_id='om_conf1'").fetchone()
            self.assertEqual(tuple(receipt), ('om_source', 'abcdef123456', 'pending_dispatch'))
            import hashlib
            receipts = Path(d) / 'receipts'
            receipts.mkdir()
            (receipts / (hashlib.sha256(b'om_source').hexdigest() + '.json')).write_text(json.dumps({
                'source': 'om_source', 'confirmation': 'om_conf1', 'commit': 'abc123', 'status': 'verified'}))
            review_cards.mark_confirmation_dispatched(db, 'om_conf1')
            self.assertEqual(review_cards.pending_confirmations(db), [])

    def test_expired_dispatching_confirmation_is_recoverable_after_worker_loss(self):
        with tempfile.TemporaryDirectory() as d:
            db = str(Path(d) / 'inbox.sqlite')
            q.init(db)
            review_cards.init(db)
            now = review_cards.time.time()
            with q.conn(db) as c:
                c.execute("""INSERT INTO confirmation_receipts
                    (message_id,sender,grp,text,source,token,status,error,attempts,next_at,created)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    ('om_lost', 'ou_a', review_cards.GROUP, '准确', 'om_source',
                     'abcdef123456', 'dispatching', 'gateway restart', 1,
                     now - 1, now - 900))

            pending = review_cards.pending_confirmations(db, now)
            self.assertEqual([row['message_id'] for row in pending], ['om_lost'])
            self.assertTrue(review_cards.claim_confirmation_dispatch(db, 'om_lost'))
            self.assertFalse(review_cards.claim_confirmation_dispatch(db, 'om_lost'))

    def test_failed_dispatch_returns_to_short_retry_queue(self):
        with tempfile.TemporaryDirectory() as d:
            db = str(Path(d) / 'inbox.sqlite')
            q.init(db)
            review_cards.init(db)
            now = review_cards.time.time()
            with q.conn(db) as c:
                c.execute("""INSERT INTO confirmation_receipts
                    (message_id,sender,grp,text,source,token,status,error,attempts,next_at,created)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    ('om_failed', 'ou_a', review_cards.GROUP, '准确', 'om_source',
                     'abcdef123456', 'dispatching', None, 1, now + 900, now))

            review_cards.mark_confirmation_dispatch_failed(db, 'om_failed', 'worker exited')
            with q.conn(db) as c:
                row = c.execute("SELECT status,error,next_at FROM confirmation_receipts WHERE message_id='om_failed'").fetchone()
            self.assertEqual(row['status'], 'pending_dispatch')
            self.assertEqual(row['error'], 'worker exited')
            self.assertLessEqual(row['next_at'], now + 31)

    def test_failed_dispatch_keeps_preflight_error_when_lease_not_started(self):
        with tempfile.TemporaryDirectory() as d:
            db = str(Path(d) / 'inbox.sqlite')
            q.init(db)
            review_cards.init(db)
            now = review_cards.time.time()
            with q.conn(db) as c:
                c.execute("""INSERT INTO confirmation_receipts
                    (message_id,sender,grp,text,source,token,status,error,attempts,next_at,created)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    ('om_preflight', 'ou_a', review_cards.GROUP, '准确', 'om_source',
                     'abcdef123456', 'pending_dispatch', None, 0, now, now))

            review_cards.mark_confirmation_dispatch_failed(
                db, 'om_preflight', '上传任务失败（code=1）：远程台账缺少个人累计区')
            with q.conn(db) as c:
                row = c.execute("SELECT status,error,attempts FROM confirmation_receipts WHERE message_id='om_preflight'").fetchone()
            self.assertEqual(row['status'], 'pending_dispatch')
            self.assertEqual(row['attempts'], 0)
            self.assertIn('远程台账缺少个人累计区', row['error'])

    def test_claim_and_failure_consume_exactly_one_attempt(self):
        with tempfile.TemporaryDirectory() as d:
            db = str(Path(d) / 'inbox.sqlite')
            review_cards.init(db)
            with q.conn(db) as c:
                c.execute("""INSERT INTO confirmation_receipts
                    (message_id,sender,grp,text,source,token,status,error,attempts,next_at,created)
                    VALUES ('om_claimed','ou_a',?,'准确','om_source','tok',
                            'pending_dispatch',NULL,0,0,0)""", (review_cards.GROUP,))
            for expected in range(1, 4):
                self.assertTrue(review_cards.claim_confirmation_dispatch(db, 'om_claimed'))
                review_cards.mark_confirmation_dispatch_failed(db, 'om_claimed', '真实失败')
                review_cards.mark_confirmation_dispatch_failed(db, 'om_claimed', '重复失败事件')
                with q.conn(db) as c:
                    row = c.execute("SELECT status,attempts FROM confirmation_receipts WHERE message_id='om_claimed'").fetchone()
                self.assertEqual(row['attempts'], expected)
                self.assertEqual(row['status'], 'blocked' if expected == 3 else 'pending_dispatch')
            self.assertFalse(review_cards.claim_confirmation_dispatch(db, 'om_claimed'))

    def test_failed_dispatch_stops_after_bounded_attempts(self):
        with tempfile.TemporaryDirectory() as d:
            db = str(Path(d) / 'inbox.sqlite')
            q.init(db)
            review_cards.init(db)
            now = review_cards.time.time()
            with q.conn(db) as c:
                c.execute("""INSERT INTO confirmation_receipts
                    (message_id,sender,grp,text,source,token,status,error,attempts,next_at,created)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    ('om_blocked', 'ou_a', review_cards.GROUP, '准确', 'om_source',
                     'abcdef123456', 'dispatching', None,
                     review_cards.MAX_DISPATCH_ATTEMPTS, now + 900, now))

            review_cards.mark_confirmation_dispatch_failed(db, 'om_blocked', 'guard failed')
            with q.conn(db) as c:
                row = c.execute("SELECT status,error,next_at FROM confirmation_receipts WHERE message_id='om_blocked'").fetchone()
            self.assertEqual(row['status'], 'blocked')
            self.assertEqual(row['error'], 'guard failed')
            self.assertGreater(row['next_at'], now + 3600)

            # A late failure callback must not reopen a quarantined receipt.
            review_cards.record_failed_confirmation(
                db, '准确', 'ou_a', review_cards.GROUP, 'om_blocked', 'late callback')
            with q.conn(db) as c:
                row = c.execute("SELECT status FROM confirmation_receipts WHERE message_id='om_blocked'").fetchone()
            self.assertEqual(row['status'], 'blocked')

    def test_verified_upload_failure_only_queues_feishu_reply(self):
        with tempfile.TemporaryDirectory() as d:
            db = str(Path(d) / 'inbox.sqlite')
            q.init(db)
            review_cards.init(db)
            now = review_cards.time.time()
            with q.conn(db) as c:
                c.execute("""INSERT INTO confirmation_receipts
                    (message_id,sender,grp,text,source,token,status,error,attempts,next_at,created)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    ('om_reply', 'ou_a', review_cards.GROUP, '准确', 'om_source',
                     'abcdef123456', 'dispatching', None,
                     review_cards.MAX_DISPATCH_ATTEMPTS, now + 60, now))
            import hashlib
            receipts = Path(d) / 'receipts'
            receipts.mkdir()
            (receipts / (hashlib.sha256(b'om_source').hexdigest() + '.json')).write_text(json.dumps({
                'source': 'om_source', 'confirmation': 'om_reply', 'commit': 'abc123',
                'status': 'verified'}))
            review_cards.mark_confirmation_dispatch_failed(db, 'om_reply', 'Feishu receipt failed')
            with q.conn(db) as c:
                row = c.execute("SELECT status,attempts FROM confirmation_receipts WHERE message_id='om_reply'").fetchone()
            self.assertEqual(row['status'], 'reply_pending')
            self.assertEqual(row['attempts'], review_cards.MAX_DISPATCH_ATTEMPTS)

    def test_verified_reply_retry_is_bounded(self):
        with tempfile.TemporaryDirectory() as d:
            db = str(Path(d) / 'inbox.sqlite')
            q.init(db)
            review_cards.init(db)
            with q.conn(db) as c:
                c.execute("""INSERT INTO confirmation_receipts
                    (message_id,sender,grp,text,source,token,status,error,attempts,next_at,created,reply_attempts)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    ('om_reply_retry', 'ou_a', review_cards.GROUP, '准确', 'om_source',
                     'abcdef123456', 'reply_pending', 'send failed', 3, 0, 0, 1))
            failed = type('Result', (), {'returncode': 1, 'stderr': 'send failed', 'stdout': ''})()
            with patch.object(review_cards, 'verify_dispatch_receipt', return_value={
                    'status': 'verified', 'source': 'om_source', 'confirmation': 'om_reply_retry',
                    'commit': 'abc123', 'message': 'ok'}), patch.object(review_cards.subprocess, 'run', return_value=failed):
                review_cards.retry_verified_replies(db, now=100)
                with q.conn(db) as c:
                    c.execute("UPDATE confirmation_receipts SET next_at=0 WHERE message_id='om_reply_retry'")
                review_cards.retry_verified_replies(db, now=100)
            with q.conn(db) as c:
                row = c.execute("SELECT status,reply_attempts FROM confirmation_receipts WHERE message_id='om_reply_retry'").fetchone()
            self.assertEqual(row['status'], 'reply_blocked')
            self.assertEqual(row['reply_attempts'], 3)

    def test_dispatch_start_records_timing_metadata(self):
        with tempfile.TemporaryDirectory() as d:
            db = str(Path(d) / 'inbox.sqlite')
            q.init(db)
            review_cards.init(db)
            now = 1700000000.0
            with q.conn(db) as c:
                c.execute("""INSERT INTO confirmation_receipts
                    (message_id,sender,grp,text,source,token,status,error,attempts,next_at,created)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    ('om_timing', 'ou_a', review_cards.GROUP, '准确', 'om_source',
                     'abcdef123456', 'dispatching', None, 1, now + 60, now - 2))
            with patch.object(review_cards.time, 'time', return_value=now):
                review_cards.mark_confirmation_dispatch_started(db, 'om_timing', 123)
            with q.conn(db) as c:
                row = c.execute("""SELECT worker_pid,dispatch_started_at,dispatched_at
                                  FROM confirmation_receipts WHERE message_id='om_timing'""").fetchone()
            self.assertEqual(row['worker_pid'], 123)
            self.assertEqual(row['dispatch_started_at'], now)
            self.assertIsNone(row['dispatched_at'])

    def test_dispatch_requires_verified_commit_receipt(self):
        with tempfile.TemporaryDirectory() as d:
            db = str(Path(d) / 'inbox.sqlite')
            q.init(db)
            review_cards.init(db)
            with self.assertRaises(ValueError):
                review_cards.verify_dispatch_receipt(db, 'om_source', 'om_confirmation')

            import hashlib
            receipts = Path(d) / 'receipts'
            receipts.mkdir()
            path = receipts / (hashlib.sha256(b'om_source').hexdigest() + '.json')
            path.write_text(json.dumps({
                'source': 'om_source', 'confirmation': 'om_confirmation',
                'commit': 'abc123', 'status': 'committed_pending_reread',
            }))
            with self.assertRaises(ValueError):
                review_cards.verify_dispatch_receipt(db, 'om_source', 'om_confirmation')
            path.write_text(json.dumps({
                'source': 'om_source', 'confirmation': 'om_confirmation',
                'commit': 'abc123', 'status': 'verified',
            }))
            result = review_cards.verify_dispatch_receipt(db, 'om_source', 'om_confirmation')
            self.assertEqual(result['commit'], 'abc123')

    def test_duplicate_confirmation_for_uploaded_source_is_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            db = str(Path(d) / 'inbox.sqlite')
            q.init(db)
            q.put(db, {
                'messageId': 'om_source', 'senderId': 'ou_a',
                'groupId': review_cards.GROUP,
                'content': '棉堆堆袜1\n冰冰袜2\n小腿袜3\n过膝袜0\n女船袜0\n男船袜0',
            })
            with q.conn(db) as c:
                result = {'extracted': {'kind': 'report', 'worker': 'A',
                    'production_date': '2026-09-24', 'items': [
                        {'product': '棉堆堆袜', 'quantity': 1}]}}
                c.execute("UPDATE inbox SET status='ready', result=? WHERE id='om_source'",
                          (json.dumps(result),))
                digest = c.execute("SELECT digest FROM inbox WHERE id='om_source'").fetchone()[0]
                c.execute('CREATE TABLE worker_identity(worker TEXT,name TEXT,platform TEXT PRIMARY KEY,owner TEXT,proof TEXT,created REAL)')
                c.execute("INSERT INTO worker_identity VALUES('A','徐超超','ou_a','test','om_source',0)")
            review_cards.init(db)
            with q.conn(db) as c:
                c.execute("""INSERT INTO review_cards(token,source,sender,grp,summary,digest,expires,state,callback,result)
                    VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    ('abcdef123456', 'om_source', 'ou_a', review_cards.GROUP,
                     '身份：A=徐超超\n合计：1', digest, 9999999999,
                     'confirmed', 'om_old_confirm', json.dumps({'source': 'om_source'})))
                c.execute("""INSERT INTO confirmation_receipts
                    (message_id,sender,grp,text,source,token,status,error,attempts,next_at,created)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    ('om_old_confirm', 'ou_a', review_cards.GROUP, '准确', 'om_source',
                     'abcdef123456', 'dispatched', None, 1, 0, 0))
            result = review_cards.act_text(db, '准确', 'ou_a', review_cards.GROUP, 'om_new_confirm')
            self.assertTrue(result['already_dispatched'])
            self.assertEqual(result['callback'], 'om_old_confirm')
            same_message = review_cards.act_text(db, '准确', 'ou_a', review_cards.GROUP, 'om_old_confirm')
            self.assertTrue(same_message['already_dispatched'])
            self.assertFalse(same_message.get('already_processing', False))
            with q.conn(db) as c:
                self.assertEqual(c.execute("SELECT count(*) FROM confirmation_receipts WHERE source='om_source'").fetchone()[0], 1)

    def test_duplicate_confirmation_after_local_verified_receipt_is_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            db = str(Path(d) / 'inbox.sqlite')
            q.init(db)
            review_cards.init(db)
            q.put(db, {
                'messageId': 'om_completed', 'senderId': 'ou_a',
                'groupId': review_cards.GROUP,
                'content': '棉堆堆袜1 冰冰袜2 小腿袜3 过膝袜0 女船袜0 男船袜0',
            })
            with q.conn(db) as c:
                result = {'extracted': {'kind': 'report', 'worker': 'A',
                    'production_date': '2026-09-28', 'items': [
                        {'product': '棉堆堆袜', 'quantity': 1}]}}
                c.execute('UPDATE inbox SET status="ready", result=? WHERE id="om_completed"',
                          (json.dumps(result),))
                digest = c.execute("SELECT digest FROM inbox WHERE id='om_completed'").fetchone()[0]
                c.execute('CREATE TABLE worker_identity(worker TEXT,name TEXT,platform TEXT PRIMARY KEY,owner TEXT,proof TEXT,created REAL)')
                c.execute("INSERT INTO worker_identity VALUES('A','徐超超','ou_a','test','om_completed',0)")
                c.execute("""INSERT INTO review_cards(token,source,sender,grp,summary,digest,expires,state,callback,result)
                    VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    ('completedtoken', 'om_completed', 'ou_a', review_cards.GROUP,
                     '身份：A=徐超超\n合计：1', digest, 9999999999,
                     'confirmed', 'om_old_confirm', json.dumps({'source': 'om_completed'})))
                c.execute("""INSERT INTO confirmation_receipts
                    (message_id,sender,grp,text,source,token,status,error,attempts,next_at,created)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    ('om_old_confirm', 'ou_a', review_cards.GROUP, '准确', 'om_completed',
                     'completedtoken', 'dispatched', None, 1, 0, 0))
            receipt_dir = Path(d) / 'receipts'
            receipt_dir.mkdir()
            import hashlib
            (receipt_dir / (hashlib.sha256(b'om_completed').hexdigest() + '.json')).write_text(json.dumps({
                'source': 'om_completed', 'confirmation': 'om_old_confirm',
                'commit': 'abc123', 'status': 'verified'}))
            result = review_cards.act_text(db, '准确', 'ou_a', review_cards.GROUP, 'om_new_confirm')
            self.assertTrue(result['already_dispatched'])
            self.assertEqual(result['callback'], 'om_old_confirm')
            with q.conn(db) as c:
                self.assertEqual(c.execute("SELECT count(*) FROM confirmation_receipts WHERE source='om_completed'").fetchone()[0], 1)

    def test_existing_confirmation_binds_reissued_pending_card(self):
        with tempfile.TemporaryDirectory() as d:
            db = str(Path(d) / 'inbox.sqlite')
            q.init(db)
            q.put(db, {
                'messageId': 'om_source', 'senderId': 'ou_a',
                'groupId': review_cards.GROUP,
                'content': '棉堆堆袜1\n冰冰袜2\n小腿袜3\n过膝袜0\n女船袜0\n男船袜0',
            })
            with q.conn(db) as c:
                result = {'extracted': {'kind': 'report', 'worker': 'A',
                    'production_date': '2026-09-24', 'items': [
                        {'product': '棉堆堆袜', 'quantity': 1}]}}
                c.execute("UPDATE inbox SET status='ready', result=? WHERE id='om_source'",
                          (json.dumps(result),))
                digest = c.execute("SELECT digest FROM inbox WHERE id='om_source'").fetchone()[0]
                c.execute('CREATE TABLE worker_identity(worker TEXT,name TEXT,platform TEXT PRIMARY KEY,owner TEXT,proof TEXT,created REAL)')
                c.execute("INSERT INTO worker_identity VALUES('A','徐超超','ou_a','test','om_source',0)")
            review_cards.init(db)
            with q.conn(db) as c:
                c.execute("""INSERT INTO review_cards(token,source,sender,grp,summary,digest,expires,state,callback,result)
                    VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    ('oldtoken12345', 'om_source', 'ou_a', review_cards.GROUP,
                     '身份：A=徐超超\n合计：1', digest, 9999999999,
                     'superseded', 'om_old_confirm', json.dumps({'source': 'om_source'})))
                c.execute("""INSERT INTO review_cards(token,source,sender,grp,summary,digest,expires,state,callback,result)
                    VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    ('newtoken12345', 'om_source', 'ou_a', review_cards.GROUP,
                     '身份：A=徐超超\n合计：1', digest, 9999999999,
                     'pending', None, None))
                c.execute("""INSERT INTO confirmation_receipts
                    (message_id,sender,grp,text,source,token,status,error,attempts,next_at,created)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    ('om_old_confirm', 'ou_a', review_cards.GROUP, '准确', 'om_source',
                     'oldtoken12345', 'pending_dispatch', None, 1, 0, 0))
                c.execute("UPDATE review_cards SET delivery='unknown' WHERE token='newtoken12345'")
            result = review_cards.act_text(db, '准确', 'ou_a', review_cards.GROUP, 'om_new_confirm')
            self.assertTrue(result['retrigger'])
            self.assertEqual(result['callback'], 'om_old_confirm')
            with q.conn(db) as c:
                row = c.execute("SELECT state,confirmation_message_id,confirmation_retry_status FROM review_cards WHERE token='newtoken12345'").fetchone()
                self.assertEqual(tuple(row), ('confirmed', 'om_old_confirm', 'bound'))
                self.assertEqual(c.execute("SELECT count(*) FROM confirmation_receipts WHERE source='om_source'").fetchone()[0], 1)

    def test_known_false_positive_blocked_confirmation_can_be_requeued(self):
        with tempfile.TemporaryDirectory() as d:
            db = str(Path(d) / 'inbox.sqlite')
            q.init(db)
            review_cards.init(db)
            with q.conn(db) as c:
                c.execute("""INSERT INTO confirmation_receipts
                    (message_id,sender,grp,text,source,token,status,error,attempts,next_at,created)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    ('om_confirm', 'ou_a', review_cards.GROUP, '准确', 'om_source',
                     'tok', 'blocked', 'ValueError: 确定性上传失败：B员工2026-09-20已有有效记录，禁止重复补报',
                     3, 9999999999, 0))
            result = review_cards.requeue_blocked_confirmation(db, 'om_confirm')
            self.assertEqual(result['status'], 'pending_dispatch')
            with q.conn(db) as c:
                row = c.execute("SELECT status,attempts,worker_pid FROM confirmation_receipts WHERE message_id='om_confirm'").fetchone()
            self.assertEqual(tuple(row), ('pending_dispatch', 0, None))

    def test_requeue_rejects_unrelated_blocked_confirmation(self):
        with tempfile.TemporaryDirectory() as d:
            db = str(Path(d) / 'inbox.sqlite')
            q.init(db)
            review_cards.init(db)
            with q.conn(db) as c:
                c.execute("""INSERT INTO confirmation_receipts
                    (message_id,sender,grp,text,source,token,status,error,attempts,next_at,created)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    ('om_confirm', 'ou_a', review_cards.GROUP, '准确', 'om_source',
                     'tok', 'blocked', 'ValueError: 原报数已改变', 3, 9999999999, 0))
            with self.assertRaisesRegex(ValueError, '已知的补报占位误判'):
                review_cards.requeue_blocked_confirmation(db, 'om_confirm')


if __name__ == '__main__':
    unittest.main()
