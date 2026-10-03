import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import queue_store as q
import review_cards
import upload_task


class UploadTaskTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.db = str(Path(self.directory.name) / 'inbox.sqlite')
        self.source = 'om_source_123'
        self.confirmation = 'om_confirmation_456'
        self.sender = 'ou_worker'
        self.group = review_cards.GROUP
        q.put(self.db, {'messageId': self.source, 'senderId': self.sender,
                       'groupId': self.group, 'content': '棉堆堆袜5000'})
        q.put(self.db, {'messageId': self.confirmation, 'senderId': self.sender,
                       'groupId': self.group, 'content': '准确'})
        review_cards.init(self.db)
        with q.conn(self.db) as conn:
            report = {'extracted': {'kind': 'report', 'worker': 'C',
                'production_date': '2026-09-28',
                'items': [{'product': '棉堆堆袜', 'quantity': 5000, 'process': '烤边'}]}}
            confirmed = {'extracted': {'kind': 'confirmation'}}
            conn.execute("UPDATE inbox SET status='ready', result=?, created=100 WHERE id=?",
                         (json.dumps(report), self.source))
            conn.execute("UPDATE inbox SET status='ready', result=?, created=101 WHERE id=?",
                         (json.dumps(confirmed), self.confirmation))
            conn.execute('CREATE TABLE worker_identity(worker TEXT,name TEXT,platform TEXT PRIMARY KEY,owner TEXT,proof TEXT,created REAL)')
            conn.execute("INSERT INTO worker_identity VALUES('C','李鸿玉',?,'test','test',0)",
                         (self.sender,))
            conn.execute('''INSERT INTO review_cards
                (token,source,sender,grp,summary,digest,expires,state,callback,result)
                VALUES(?,?,?,?,?,?,?,?,?,?)''',
                ('token_123', self.source, self.sender, self.group, 'summary',
                 'digest', 9999999999, 'confirmed', self.confirmation, '{}'))
            conn.execute('''INSERT INTO confirmation_receipts
                (message_id,sender,grp,text,source,token,status,next_at,created)
                VALUES(?,?,?,?,?,?,?,?,?)''',
                (self.confirmation, self.sender, self.group, '准确', self.source,
                 'token_123', 'pending_dispatch', 0, 101))

    def prepare(self):
        return upload_task.prepare(self.db, self.source, self.confirmation, self.group)

    def test_exact_pair_prepares_private_immutable_task_and_inspects(self):
        result = self.prepare()
        path = Path(result['task'])
        self.assertEqual(path.parent, Path(self.db).resolve().parent / 'upload-tasks')
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        first_stat = path.stat()
        self.assertEqual(self.prepare()['task'], str(path))
        self.assertEqual(path.stat().st_ino, first_stat.st_ino)
        inspected = upload_task.inspect(self.db, path)
        self.assertTrue(inspected['validated'])
        self.assertEqual(inspected['confirmation_record']['id'], self.confirmation)
        self.assertEqual(inspected['report']['worker'], 'C')

    def test_typo_id_is_rejected_and_never_creates_task(self):
        with self.assertRaisesRegex(ValueError, '没有持久化确认回执'):
            upload_task.prepare(self.db, self.source, 'om_confirmation_465', self.group)
        self.assertFalse(upload_task.task_directory(self.db).exists())

    def test_conflicting_receipt_source_group_sender_and_status_are_rejected(self):
        for column, value in [('source', 'om_other'), ('grp', 'oc_other'),
                              ('sender', 'ou_other'), ('status', 'blocked')]:
            with self.subTest(column=column):
                with q.conn(self.db) as conn:
                    original = conn.execute('SELECT ' + column + ' FROM confirmation_receipts').fetchone()[0]
                    conn.execute('UPDATE confirmation_receipts SET ' + column + '=?', (value,))
                with self.assertRaises(ValueError):
                    self.prepare()
                with q.conn(self.db) as conn:
                    conn.execute('UPDATE confirmation_receipts SET ' + column + '=?', (original,))

    def test_unready_confirmation_cannot_pass_preflight(self):
        with q.conn(self.db) as conn:
            conn.execute("UPDATE inbox SET status='queued' WHERE id=?", (self.confirmation,))
        with self.assertRaisesRegex(ValueError, '尚未完成结构化核对'):
            self.prepare()

    def test_existing_conflicting_task_cannot_be_overwritten(self):
        task = self.prepare()
        path = Path(task['task'])
        path.write_text('{"schema":"wrong"}')
        with self.assertRaisesRegex(ValueError, '禁止覆盖'):
            self.prepare()
        self.assertEqual(path.read_text(), '{"schema":"wrong"}')

    def test_modified_task_and_foreign_path_are_rejected(self):
        task = self.prepare()
        path = Path(task['task'])
        changed = json.loads(path.read_text())
        changed['confirmation'] = 'om_confirmation_465'
        path.write_text(json.dumps(changed))
        with self.assertRaisesRegex(ValueError, '文件名'):
            upload_task.inspect(self.db, path)
        with self.assertRaisesRegex(ValueError, 'upload-tasks'):
            upload_task.inspect(self.db, Path(self.directory.name) / 'outside.json')

    def test_commit_cli_accepts_task_without_manual_message_ids_and_uses_guard(self):
        task = self.prepare()
        with patch.object(upload_task, 'DB', self.db), \
             patch.object(upload_task.subprocess, 'run') as run:
            upload_task.main(['commit', '--task', task['task'], '--expected-sha', 'sha123',
                              '--file', 'candidate.md', '--review-note', 'checked'])
        args = run.call_args.args[0]
        self.assertEqual(args[1], str(Path(upload_task.commit_guard.__file__).resolve()))
        self.assertEqual(args[args.index('--source') + 1], self.source)
        self.assertEqual(args[args.index('--confirmation') + 1], self.confirmation)
        self.assertEqual(args[args.index('--expected-sha') + 1], 'sha123')
        self.assertEqual(run.call_args.kwargs['env']['PRODUCTION_INBOX_DB'], str(Path(self.db).resolve()))
        self.assertTrue(run.call_args.kwargs['check'])

    def test_commit_revalidates_durable_binding_before_starting_guard(self):
        task = self.prepare()
        with q.conn(self.db) as conn:
            conn.execute("UPDATE confirmation_receipts SET source='om_other'")
        with patch.object(upload_task.subprocess, 'run') as run:
            with self.assertRaises(ValueError):
                upload_task.commit(self.db, task['task'], 'sha123', 'candidate.md', 'checked')
        run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
