"""2027 chain acceptance in temporary state; platform I/O is always mocked."""
import base64
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

import commit_guard
import deterministic_upload as writer
import hermes_extract
import historical_supplement
import queue_store as queue
import recovery_scan
import review_cards
import service
import upload_task
import worker_identity

HERE = Path(__file__).resolve().parent
GUARDS = HERE.parents[1] / '袜子生产制造袜子厂'
if str(GUARDS) not in sys.path:
    sys.path.insert(0, str(GUARDS))
import guard_production_ledger
import production_summary_guard


class AnnualChainTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = str(Path(directory.name) / 'inbox.sqlite')
        queue.init(self.db)
        review_cards.init(self.db)
        self.environment = patch.dict(os.environ, {'PRODUCTION_LEDGER_ENDPOINT': ''})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.before = (HERE / 'templates/2027全年下机白胚半成品统计.md').read_text(encoding='utf-8')

    def bind(self, worker, sender):
        worker_identity.init(self.db)
        with queue.conn(self.db) as conn:
            conn.execute('INSERT INTO worker_identity VALUES(?,?,?,?,?,?)',
                         (worker, worker_identity.NAMES[worker], sender, 'isolated', 'fixture', 0))

    def ready(self, event, extracted, created):
        queue.put(self.db, event)
        with queue.conn(self.db) as conn:
            conn.execute("UPDATE inbox SET status='ready', result=?, created=? WHERE id=?",
                         (json.dumps({'extracted': extracted}, ensure_ascii=False),
                          created, event['messageId']))

    def local_guard(self, before, candidate):
        with contextlib.redirect_stdout(io.StringIO()):
            guard_production_ledger.check(before, candidate)
        production_summary_guard.check_daily_summary(candidate)
        return 'isolated original and daily guards passed'

    def test_normal_january_and_december_complete_one_bound_upload_and_receipt(self):
        # A later confirmation timestamp must never move the source to a
        # different production year. Both platform transports are fake.
        for day, worker in [('2027-01-01', 'B'), ('2027-12-31', 'D')]:
            with self.subTest(day=day), tempfile.TemporaryDirectory() as directory:
                self.db = str(Path(directory) / 'inbox.sqlite')
                queue.init(self.db)
                review_cards.init(self.db)
                source = 'om_annual' + day.replace('-', '')
                confirmation = source + 'confirmed'
                sender = 'ou_isolated_' + worker
                self.bind(worker, sender)
                values = dict(zip(writer.PRODUCTS, [19, 23, 7, 0, 3, 0]))
                event = {'messageId': source, 'senderId': sender,
                         'groupId': review_cards.GROUP,
                         'content': f'{worker}={worker_identity.NAMES[worker]}\n生产日期：{day}\n'
                                    + '\n'.join(f'{product}：{quantity}' for product, quantity in values.items()),
                         'timestamp': day + 'T20:00:00+08:00'}
                extracted = service.fast_extract(event)['extracted']
                self.assertEqual(extracted['production_date'], day)
                self.ready(event, extracted, 100)
                record = queue.get(self.db, source)
                record['_db'] = self.db
                card = review_cards.issue(self.db, source, recovery_scan._summary(record))
                posts = []
                transport = ModuleType('transport')

                def fake_request(agent, method, path, body):
                    self.assertEqual((agent, method), ('xiaowen', 'POST'))
                    self.assertEqual(body['receive_id'], review_cards.GROUP)
                    posts.append(body)
                    return {'data': {'message_id': 'om_isolated_card' if len(posts) == 1 else 'om_isolated_receipt'}}

                transport.request = fake_request
                with patch.dict(sys.modules, {'transport': transport}):
                    self.assertEqual(review_cards.deliver_card(self.db, card['token'])['status'], 'sent')
                    confirmation_event = dict(event, messageId=confirmation,
                                              content='准确', timestamp='2028-01-02T10:00:00+08:00')
                    self.ready(confirmation_event, hermes_extract.extract(confirmation_event)['extracted'], 101)
                    bound = review_cards.act_text(self.db, '准确', sender, review_cards.GROUP, confirmation)
                    self.assertEqual(bound['source'], source)
                    task = upload_task.prepare(self.db, source, confirmation, review_cards.GROUP)
                    self.assertEqual(task['ledger_endpoint'], commit_guard.LEDGER_ENDPOINTS[2027])
                    with patch.object(writer, 'DB', self.db):
                        _, report = writer._standard_report(task['task'])
                    candidate = writer.build_candidate(self.before, report, source, confirmation)
                    self.local_guard(self.before, candidate)
                    log_start = candidate.index('### 更新记录')
                    entry = next(line for line in candidate[log_start:].splitlines()
                                 if line.startswith('| ' + day + ' |') and source in line)
                    self.assertEqual(len(writer._row_parts(entry)), 4)
                    self.assertEqual(writer._row_parts(entry)[-1], '见本次成功回执')
                    self.assertFalse(any(product in entry for product in writer.PRODUCTS))
                    self.assertIn('来源消息：' + source, candidate[:log_start])
                    self.assertIn('确认消息：' + confirmation, candidate[:log_start])
                    remote = {'text': self.before, 'sha': 'before'}
                    reads = []
                    puts = []

                    def fake_read(endpoint):
                        self.assertEqual(endpoint, commit_guard.LEDGER_ENDPOINTS[2027])
                        reads.append(endpoint)
                        return {'sha': remote['sha'],
                                'content': base64.b64encode(remote['text'].encode()).decode()}

                    def fake_put(args, **kwargs):
                        self.assertEqual(args[1:4], ['api', '--method', 'PUT'])
                        self.assertEqual(args[4], commit_guard.LEDGER_ENDPOINTS[2027])
                        payload = json.loads(kwargs['input'])
                        self.assertEqual(payload['sha'], 'before')
                        remote.update(text=base64.b64decode(payload['content']).decode(), sha='after')
                        puts.append(payload)
                        return SimpleNamespace(returncode=0,
                                               stdout=json.dumps({'commit': {'sha': 'isolated_commit'}}))

                    receipt_name = hashlib.sha256(source.encode()).hexdigest() + '.json'
                    with patch.object(commit_guard, 'DB', self.db), \
                         patch.object(commit_guard, 'gh_read_json', side_effect=fake_read), \
                         patch.object(commit_guard, 'run_original_ledger_guard', side_effect=self.local_guard), \
                         patch.object(commit_guard.subprocess, 'run', side_effect=fake_put), \
                         contextlib.redirect_stdout(io.StringIO()):
                        commit_guard.write_remote(None, candidate, source, confirmation, 'isolated',
                                                  receipt_name, True, 'before', 'six values remotely reread')
                    self.assertEqual(remote['text'], candidate)
                    self.assertEqual(len(puts), 1)
                    self.assertEqual(len(reads), 3)
                    receipt = review_cards.verify_dispatch_receipt(self.db, source, confirmation)
                    self.assertEqual(receipt['status'], 'verified')
                    self.assertIn(f'生产日：{day}', receipt['message'])
                    self.assertIn('已更新：2027全年下机白胚半成品统计.md', receipt['message'])
                    spec = importlib.util.spec_from_file_location(
                        'isolated_send_upload_receipt', HERE.parent / 'group-companion/send_upload_receipt.py')
                    receipt_sender = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(receipt_sender)
                    with patch.object(sys, 'argv', ['send_upload_receipt.py', '--group', review_cards.GROUP,
                                                  '--receipt-json', json.dumps(receipt)]), \
                         contextlib.redirect_stdout(io.StringIO()):
                        receipt_sender.main()
                        receipt_sender.main()
                    # Repeat delivery uses Feishu's same idempotency key.
                    self.assertEqual(posts[-1]['uuid'], posts[-2]['uuid'])
                    self.assertIn(day, posts[-1]['content'])
                    review_cards.mark_confirmation_dispatched(self.db, confirmation, 'om_isolated_receipt')
                    repeated = review_cards.act_text(self.db, '准确', sender, review_cards.GROUP,
                                                     confirmation + 'again')
                    self.assertTrue(repeated['already_dispatched'])
                    with patch.object(writer, 'DB', self.db), \
                         patch.object(writer, '_remote', side_effect=AssertionError('repeat must not write again')), \
                         contextlib.redirect_stdout(io.StringIO()):
                        self.assertEqual(writer.run(task['task'])['commit'], 'isolated_commit')
                    self.assertEqual(len(puts), 1)

    def test_first_d_record_and_each_personal_month_stay_inside_history_appendix(self):
        report = {'worker': 'D', 'name': '张小翠', 'process': '烤边',
                  'production_date': '2027-01-01',
                  'values': dict(zip(writer.PRODUCTS, [2, 3, 4, 0, 0, 0]))}
        candidate = writer.build_candidate(self.before, report, 'om_firstD', 'om_firstDconfirm')
        candidate = writer.build_candidate(candidate, dict(report, production_date='2027-12-31'),
                                           'om_lastD', 'om_lastDconfirm')
        self.local_guard(self.before, candidate)
        history_start = candidate.index('<summary>附录：历史个人生产明细')
        audit_start = candidate.index('## 附录：审计与版本记录')
        depth = 1
        close = None
        for tag in re.finditer(r'<details>|</details>', candidate[history_start:audit_start]):
            depth += -1 if tag.group() == '</details>' else 1
            if depth == 0:
                close = history_start + tag.start()
                break
        self.assertIsNotNone(close)
        for label in ['### 2027-01-01｜张小翠烤边', '### 2027-12-31｜张小翠烤边',
                      '### D｜2027年1月个人累计', '### D｜2027年12月个人累计']:
            self.assertLess(candidate.index(label), close)
        # Normal uploads may append update-log entries, while existing audit
        # template text and rows must survive the history append operation.
        for line in self.before[self.before.index('## 附录：审计与版本记录'):].splitlines():
            if line.strip():
                # Current-version rows are derived from the release manifest;
                # historical audit rows remain byte-for-byte protected.
                if re.match(r'^\| (GitHub|OpenClaw|飞书) \| \*\*', line):
                    line = re.sub(r'\*\*V[^*]+\*\*', '**'+writer.coverage_tables.version_info()['workbench_version']+'**', line, count=1)
                self.assertIn(line, candidate[audit_start:])

    def test_new_year_cutoff_and_explicit_history_are_routed_by_production_day(self):
        text = 'B=梅芳\n' + '\n'.join(f'{p}：1' for p in writer.PRODUCTS)
        for timestamp, prefix, expected in [
            ('2027-01-01T06:59:59+08:00', '', '2026-12-31'),
            ('2027-01-01T07:00:00+08:00', '', '2027-01-01'),
            ('2027-01-01T20:00:00+08:00', '补昨天\n', '2026-12-31'),
            ('2028-01-01T06:59:59+08:00', '', '2027-12-31'),
            ('2028-01-01T20:00:00+08:00', '补报日期：2027-12-31\n', '2027-12-31'),
        ]:
            with self.subTest(timestamp=timestamp, prefix=prefix):
                report = service.fast_extract({'content': prefix + text, 'timestamp': timestamp})['extracted']
                self.assertEqual(report['production_date'], expected)
                self.assertEqual(commit_guard.endpoint_for_date(expected),
                                 commit_guard.LEDGER_ENDPOINTS[int(expected[:4])])

    def test_2027_query_messages_are_read_only_not_production_cards(self):
        for content in ['查询2027年1月徐超超产能', '汇总2027年12月全部员工产能',
                        '分析2026年12月31日到2027年1月1日的历史产量']:
            event = {'content': content, 'timestamp': '2028-01-01T20:00:00+08:00'}
            self.assertIsNone(service.fast_extract(event))
            self.assertIsNone(hermes_extract.deterministic_report(event))

    def test_2027_partial_zero_recovery_preserves_known_values_and_unique_task(self):
        fixture = '''## 每日汇总
### 2027年1月每日汇总
#### 2027-01-20
| A｜徐超超 | 下机 | 17 | 23 | 5 | 核实 | 核实 | 核实 | 45 |
## A｜徐超超下机
### 2027-01-20｜徐超超下机
| 20270120-A-001 | 棉堆堆袜 | 17 | 已确认 |
| 20270120-A-002 | 冰冰袜 | 23 | 已确认 |
| 20270120-A-003 | 小腿袜 | 5 | 已确认 |
## C｜李鸿玉烤边
'''
        self.bind('A', 'ou_isolated_A')
        endpoints = []
        def fake_read(endpoint):
            endpoints.append(endpoint)
            return {'content': base64.b64encode(fixture.encode()).decode()}
        with patch.object(service, 'DB', self.db), \
             patch.object(commit_guard, 'gh_read_json', side_effect=fake_read), \
             patch.object(recovery_scan, 'deliver'):
            for index in (1, 2):
                event = {'messageId': f'om_supplement{index}', 'senderId': 'ou_isolated_A',
                         'groupId': review_cards.GROUP,
                         'content': '2027-01-20过膝袜0，女船袜0，男船袜0',
                         'timestamp': '2028-01-01T20:00:00+08:00'}
                self.ready(event, service.fast_extract(event)['extracted'], index)
                recovery_scan.scan(self.db)
        self.assertEqual(endpoints, [commit_guard.LEDGER_ENDPOINTS[2027]] * 2)
        merged = queue.get(self.db, 'om_supplement1')['result']['extracted']
        self.assertEqual(merged['total'], 45)
        self.assertEqual([i['quantity'] for i in merged['items']], [17, 23, 5, 0, 0, 0])
        self.assertEqual(merged['production_date'], '2027-01-20')
        with queue.conn(self.db) as conn:
            cards = conn.execute('SELECT summary FROM review_cards').fetchall()
        self.assertEqual(len(cards), 1)
        self.assertIn('保留原记录', cards[0]['summary'])
        invalid = queue.get(self.db, 'om_supplement1')
        with self.assertRaises(ValueError):
            historical_supplement.validate_supplement_summary(
                invalid, '生产日：2027-01-20\n' + '\n'.join(p + '：0' for p in writer.PRODUCTS) + '\n合计：0')

    def test_revoked_2027_source_cannot_replay_and_generate_another_card(self):
        event = {'messageId': 'om_revoked2027', 'senderId': 'ou_isolated_A',
                 'groupId': review_cards.GROUP, 'content': '2027-01-20 过膝袜0'}
        queue.put(self.db, event)
        queue.block_replay(self.db, [event['messageId']])
        with queue.conn(self.db) as conn:
            conn.execute('DELETE FROM inbox WHERE id=?', (event['messageId'],))
        self.assertIsNone(queue.put(self.db, event))
        self.assertIsNone(queue.get(self.db, event['messageId']))
        with patch.object(recovery_scan, 'deliver'), \
             patch.object(commit_guard, 'gh_read_json', side_effect=AssertionError('no remote I/O')):
            self.assertEqual(recovery_scan.scan(self.db), [])

    def test_archived_2027_card_cannot_be_sent_or_confirmed_again(self):
        sender = 'ou_isolated_B'
        self.bind('B', sender)
        event = {'messageId': 'om_archived2027', 'senderId': sender,
                 'groupId': review_cards.GROUP,
                 'content': 'B=梅芳\n2027-01-20\n' + '\n'.join(p + '：1' for p in writer.PRODUCTS),
                 'timestamp': '2027-01-20T20:00:00+08:00'}
        self.ready(event, service.fast_extract(event)['extracted'], 100)
        record = queue.get(self.db, event['messageId'])
        record['_db'] = self.db
        card = review_cards.issue(self.db, event['messageId'], recovery_scan._summary(record))
        with queue.conn(self.db) as conn:
            conn.execute("UPDATE review_cards SET state='archived' WHERE token=?", (card['token'],))
        self.assertEqual(review_cards.deliver_card(self.db, card['token'])['status'], 'skipped')
        with self.assertRaisesRegex(ValueError, '不存在或已失效'):
            review_cards.act_text(self.db, '准确 ' + event['messageId'], sender,
                                  review_cards.GROUP, 'om_archived_confirm')
        with queue.conn(self.db) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM confirmation_receipts').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
