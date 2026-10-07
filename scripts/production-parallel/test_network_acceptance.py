"""Cross-stage outage acceptance using temporary state and fake platform I/O.

These tests exercise production parsers, confirmation binding, ledger guards,
writer receipts and reply retries. They never call GitHub, Feishu or a model.
The fake Feishu server models UUID deduplication; real platform delivery remains
an external acceptance boundary, not a guarantee made by these tests.
"""
import base64
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

import commit_guard
import deterministic_upload as writer
import hermes_extract
import queue_store as queue
import recovery_scan
import review_cards
import service
import upload_task
import worker_identity

HERE = Path(__file__).resolve().parent
GUARDS = HERE.parents[1] / '袜子生产制造袜子厂'
sys.path.insert(0, str(GUARDS))
import guard_production_ledger
import production_summary_guard


class NetworkAcceptanceTests(unittest.TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.db = str(Path(root.name) / 'inbox.sqlite')
        self.source = 'om_isolated_network_source'
        self.confirmation = 'om_isolated_network_confirmation'
        self.sender = 'ou_isolated_network_worker'
        self.puts = 0
        self.remote = {}
        self.posts = []
        self.logical_messages = {}
        for target, name, value in [
            (service, 'DB', self.db), (writer, 'DB', self.db),
            (commit_guard, 'DB', self.db),
        ]:
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        environment = patch.dict(os.environ, {'PRODUCTION_INBOX_DB': self.db,
                                               'PRODUCTION_LEDGER_ENDPOINT': ''})
        environment.start()
        self.addCleanup(environment.stop)
        network = patch.object(socket.socket, 'connect', side_effect=AssertionError('real network forbidden'))
        network.start()
        self.addCleanup(network.stop)
        transport = ModuleType('transport')
        transport.request = self.feishu
        module = patch.dict(sys.modules, {'transport': transport})
        module.start()
        self.addCleanup(module.stop)
        spec = importlib.util.spec_from_file_location(
            'isolated_network_receipt', HERE.parent / 'group-companion/send_upload_receipt.py')
        self.receipt_sender = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.receipt_sender)

    def guard(self, before, candidate):
        with contextlib.redirect_stdout(io.StringIO()):
            guard_production_ledger.check(before, candidate)
        production_summary_guard.check_daily_summary(candidate)
        return 'original ledger and daily summary guards passed'

    def ready(self, event, result):
        queue.put(self.db, event)
        job = queue.claim(self.db)
        self.assertEqual(job['id'], event['messageId'])
        queue.finish(self.db, job['id'], job['lease'], result)

    def prepare(self, year=2027, worker='B', missing=()):
        queue.init(self.db)
        worker_identity.init(self.db)
        with queue.conn(self.db) as c:
            c.execute('INSERT INTO worker_identity VALUES(?,?,?,?,?,?)',
                      (worker, worker_identity.NAMES[worker], self.sender, 'fixture', 'fixture', 0))
        day = f'{year}-12-31'
        content = f'{worker}={worker_identity.NAMES[worker]}\n'
        # Explicit-date partial rows intentionally belong to historical
        # supplement review; test ordinary omitted items via the source time.
        if not missing:
            content += f'生产日期：{day}\n'
        content += '\n'.join(f'{p}：{n}' for p, n in zip(writer.PRODUCTS, [19, 23, 7, 0, 3, 0]) if p not in missing)
        event = dict(messageId=self.source, senderId=self.sender, groupId=review_cards.GROUP,
                     content=content, timestamp=day + 'T20:00:00+08:00')
        self.ready(event, service.fast_extract(event))
        record = queue.get(self.db, self.source)
        record['_db'] = self.db
        card = review_cards.issue(self.db, self.source, recovery_scan._summary(record))
        display = '下机翻袜产量' if worker in ('A', 'B') else '烤边产量'
        self.assertIn(display, json.dumps(card['card'], ensure_ascii=False))
        self.assertNotIn('网络恢复后补处理', json.dumps(card['card'], ensure_ascii=False))
        self.assertEqual(review_cards.deliver_card(self.db, card['token'])['status'], 'sent')
        confirmed = dict(event, messageId=self.confirmation, content='准确')
        self.ready(confirmed, hermes_extract.extract(confirmed))
        self.assertEqual(review_cards.act_text(self.db, '准确', self.sender, review_cards.GROUP,
                                              self.confirmation)['source'], self.source)
        task = upload_task.prepare(self.db, self.source, self.confirmation, review_cards.GROUP)
        self.task = task['task']
        _, self.report = writer._standard_report(self.task)
        self.endpoint = commit_guard.LEDGER_ENDPOINTS[year]
        self.assertEqual(task['ledger_endpoint'], self.endpoint)
        template = (HERE / 'templates/2027全年下机白胚半成品统计.md' if year == 2027 else
                    GUARDS / '库存记录/2026下半年下机白胚半成品统计.md')
        self.before = template.read_text(encoding='utf-8')
        self.candidate = writer.build_candidate(self.before, self.report, self.source, self.confirmation)
        self.guard(self.before, self.candidate)
        self.remote.update(text=self.before, sha='before')
        self.receipt_name = hashlib.sha256(self.source.encode()).hexdigest() + '.json'
        self.receipt_path = Path(self.db).parent / 'receipts' / self.receipt_name
        self.assertTrue(review_cards.claim_confirmation_dispatch(self.db, self.confirmation))

    def read(self, endpoint):
        if '/commits?path=' in endpoint:
            return [{'sha': 'isolated_commit'}]
        self.assertEqual(endpoint, self.endpoint)
        return {'sha': self.remote['sha'], 'content': base64.b64encode(self.remote['text'].encode()).decode()}

    def put(self, args, **kwargs):
        self.assertEqual(args[1:5], ['api', '--method', 'PUT', self.endpoint])
        payload = json.loads(kwargs['input'])
        self.assertEqual(payload['sha'], self.remote['sha'])
        self.remote.update(text=base64.b64decode(payload['content']).decode(), sha='after')
        self.puts += 1
        return SimpleNamespace(returncode=0, stdout=json.dumps({'commit': {'sha': 'isolated_commit'}}))

    def write(self, read=None, put=None):
        commit_guard.validate(self.db, self.source, self.confirmation)
        with patch.object(commit_guard, 'gh_read_json', side_effect=read or self.read), \
             patch.object(commit_guard, 'run_original_ledger_guard', side_effect=self.guard), \
             patch.object(commit_guard.subprocess, 'run', side_effect=put or self.put), \
             contextlib.redirect_stdout(io.StringIO()):
            commit_guard.write_remote(None, self.candidate, self.source, self.confirmation,
                                      'isolated fixture', self.receipt_name, True, 'before', 'six values reread')

    def feishu(self, agent, method, path, body):
        self.assertEqual((agent, method), ('xiaowen', 'POST'))
        self.posts.append(body)
        key = body['uuid']
        self.logical_messages.setdefault(key, 'om_isolated_' + str(len(self.logical_messages)))
        return {'data': {'message_id': self.logical_messages[key]}}

    def send_receipt(self):
        output = io.StringIO()
        with patch.object(sys, 'argv', ['send_upload_receipt.py', '--group', review_cards.GROUP,
                                       '--receipt', str(self.receipt_path)]), contextlib.redirect_stdout(output):
            self.receipt_sender.main()
        return json.loads(output.getvalue())

    def test_both_years_all_four_workers_confirm_write_readback_and_one_receipt(self):
        # Separate TestCase fixtures guarantee no cross-worker state leakage.
        for year in (2026, 2027):
            for worker in 'ABCD':
                with self.subTest(year=year, worker=worker):
                    case = NetworkAcceptanceTests()
                    case.setUp()
                    try:
                        case.prepare(year, worker)
                        case.write()
                        receipt = review_cards.verify_dispatch_receipt(case.db, case.source, case.confirmation)
                        display = '下机翻袜产量' if worker in 'AB' else '烤边产量'
                        self.assertIn(display, receipt['message'])
                        self.assertIn(f'• 工序：{case.report["process"]}', receipt['message'])
                        reply = case.send_receipt()
                        review_cards.mark_confirmation_dispatched(case.db, case.confirmation, reply['message_id'])
                        self.assertEqual(review_cards.retry_verified_replies(case.db), [])
                        self.assertTrue(review_cards.act_text(case.db, '准确', case.sender, review_cards.GROUP,
                                                             case.confirmation + '_again')['already_dispatched'])
                        with patch.object(writer, '_remote', side_effect=AssertionError('duplicate read/write forbidden')), \
                             contextlib.redirect_stdout(io.StringIO()):
                            writer.run(case.task)
                        self.assertEqual(case.puts, 1)
                        self.assertEqual(len(case.logical_messages), 2)  # One review plus one success receipt.
                    finally:
                        case.doCleanups()

    def test_outage_before_write_keeps_confirmation_and_resumes_once(self):
        self.prepare()
        with self.assertRaisesRegex(TimeoutError, 'network unavailable'):
            self.write(read=lambda endpoint: (_ for _ in ()).throw(TimeoutError('network unavailable')))
        self.assertEqual(self.puts, 0)
        self.assertFalse(self.receipt_path.exists())
        review_cards.mark_confirmation_dispatch_failed(self.db, self.confirmation, 'network unavailable')
        with queue.conn(self.db) as c:
            row = c.execute('SELECT source,status FROM confirmation_receipts').fetchone()
        self.assertEqual(tuple(row), (self.source, 'pending_dispatch'))
        self.write()
        self.assertEqual(self.puts, 1)

    def test_put_accepted_but_response_lost_recovers_without_second_write(self):
        for missing in [(), ('男船袜', '过膝袜')]:
            with self.subTest(missing=missing):
                case = NetworkAcceptanceTests()
                case.setUp()
                try:
                    case.prepare(missing=missing)
                    def lost_response(args, **kwargs):
                        case.put(args, **kwargs)
                        return SimpleNamespace(returncode=1, stdout='', stderr='connection reset after acceptance')
                    with self.assertRaisesRegex(RuntimeError, '上传失败'):
                        case.write(put=lost_response)
                    self.assertFalse(case.receipt_path.exists())
                    with patch.object(commit_guard, 'gh_read_json', side_effect=case.read), \
                         patch.object(writer.subprocess, 'run', side_effect=AssertionError('second write forbidden')), \
                         contextlib.redirect_stdout(io.StringIO()):
                        receipt = writer.run(case.task)
                        self.assertEqual(writer.run(case.task), receipt)
                    self.assertEqual(receipt['status'], 'verified')
                    self.assertEqual(receipt['report']['missing_products'], [p for p in writer.PRODUCTS if p in missing])
                    self.assertEqual(receipt['report']['values'], case.report['values'])
                    self.assertIsInstance(case.report['missing_products'], set)
                    reply = case.send_receipt()
                    review_cards.mark_confirmation_dispatched(case.db, case.confirmation, reply['message_id'])
                    self.assertEqual(review_cards.retry_verified_replies(case.db), [])
                    self.assertEqual(len(case.logical_messages), 2)
                    self.assertEqual(case.puts, 1)
                finally:
                    case.doCleanups()

    def test_write_succeeded_reread_outage_stays_unverified_until_recovery(self):
        self.prepare()
        def interrupted_read(endpoint):
            if self.puts:
                raise TimeoutError('network lost before readback')
            return self.read(endpoint)
        with self.assertRaisesRegex(RuntimeError, '回读未核实'):
            self.write(read=interrupted_read)
        self.assertEqual(json.loads(self.receipt_path.read_text())['status'], 'committed_pending_reread')
        with self.assertRaisesRegex(ValueError, '尚未完成远程回读'):
            review_cards.verify_dispatch_receipt(self.db, self.source, self.confirmation)
        with patch.object(commit_guard, 'gh_read_json', side_effect=self.read), \
             patch.object(commit_guard.subprocess, 'check_output', return_value='isolated_commit\n'):
            receipt = commit_guard.recover_pending_receipt(self.source, self.confirmation)
        self.assertEqual(receipt['status'], 'verified')
        self.assertEqual(self.puts, 1)

    def test_receipt_response_lost_retries_same_uuid_without_reupload(self):
        self.prepare()
        self.write()
        def lost_response(*args, **kwargs):
            self.feishu(*args, **kwargs)
            raise TimeoutError('receipt accepted; response lost')
        with patch.object(self.receipt_sender, 'request', side_effect=lost_response):
            with self.assertRaises(TimeoutError):
                self.send_receipt()
        review_cards.mark_confirmation_dispatch_failed(self.db, self.confirmation, 'receipt response lost')
        with queue.conn(self.db) as c:
            self.assertEqual(c.execute('SELECT status FROM confirmation_receipts').fetchone()[0], 'reply_pending')
        def send_only(args, **kwargs):
            self.assertIn('send_upload_receipt.py', args[1])
            output = self.send_receipt()
            return SimpleNamespace(returncode=0, stdout=json.dumps(output), stderr='')
        with patch.object(review_cards.subprocess, 'run', side_effect=send_only):
            self.assertEqual(review_cards.retry_verified_replies(self.db, now=10**12)[0]['action'], 'reply_dispatched')
            self.assertEqual(review_cards.retry_verified_replies(self.db, now=10**12), [])
        self.assertEqual(self.posts[-1]['uuid'], self.posts[-2]['uuid'])
        self.assertEqual(len(self.logical_messages), 2)
        self.assertEqual(self.puts, 1)

    def test_remote_revision_change_is_blocked_before_put(self):
        self.prepare()
        reads = 0
        def changed_remote(endpoint):
            nonlocal reads
            reads += 1
            result = self.read(endpoint)
            if reads == 2:
                result['sha'] = 'concurrent_writer'
            return result
        with self.assertRaisesRegex(ValueError, '提交前远程已更新'):
            self.write(read=changed_remote)
        self.assertEqual(self.puts, 0)


if __name__ == '__main__':
    unittest.main()
