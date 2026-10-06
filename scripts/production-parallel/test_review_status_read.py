"""Keep transient SQLite failures from becoming a falsely empty card queue."""
import base64
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import missing_alerts


class ReviewStatusReadTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        # URI metacharacters must remain part of the database filename.
        self.db = Path(self.temporary.name) / 'review ?cards#.sqlite'
        connection = sqlite3.connect(self.db)
        try:
            connection.execute(
                'CREATE TABLE review_cards '
                '(summary TEXT, delivery TEXT, state TEXT, card_kind TEXT)'
            )
            connection.executemany('INSERT INTO review_cards VALUES(?,?,?,?)', [
                ('身份：A=徐超超\n生产日：2026-12-31\n棉堆堆袜：1双',
                 'sent', 'pending', 'production'),
                ('身份：B=梅芳\n生产日：2027-01-01\n冰冰袜：2双',
                 'sent', 'pending', 'production'),
                ('身份：C=李鸿玉\n生产日：2027-01-02\n小腿袜：3双',
                 'sent', 'uploaded', 'production'),
                ('身份：D=张小翠\n生产日：2027-01-02',
                 'sent', 'pending', 'status'),
            ])
            connection.commit()
        finally:
            connection.close()

    def test_uri_filename_and_year_selection_preserve_only_active_cards(self):
        current = missing_alerts.review_status_section(str(self.db), 2027)
        self.assertIn('| B｜梅芳 | 2027-01-01 |', current)
        self.assertNotIn('2026-12-31', current)
        self.assertNotIn('李鸿玉', current)
        self.assertNotIn('张小翠', current)
        previous = missing_alerts.review_status_section(str(self.db), 2026)
        self.assertIn('| A｜徐超超 | 2026-12-31 |', previous)
        self.assertNotIn('2027-01-01', previous)

    def test_success_closes_real_connection(self):
        original = sqlite3.connect
        opened = []

        def tracked_connect(*args, **kwargs):
            connection = original(*args, **kwargs)
            opened.append(connection)
            return connection

        with patch.object(missing_alerts.sqlite3, 'connect', side_effect=tracked_connect):
            missing_alerts._review_status_rows(str(self.db))
        self.assertEqual(len(opened), 1)
        with self.assertRaises(sqlite3.ProgrammingError):
            opened[0].execute('SELECT 1')

    def test_existing_wal_snapshot_is_readable_but_reader_cannot_change_business_rows(self):
        original = sqlite3.connect
        writer = original(self.db)
        checks = []
        test = self

        class GuardedConnection(sqlite3.Connection):
            def execute(self, statement, parameters=()):
                if statement.startswith('SELECT summary, delivery'):
                    test.assertEqual(super().execute('PRAGMA query_only').fetchone()[0], 1)
                    with test.assertRaisesRegex(sqlite3.OperationalError, 'readonly'):
                        super().execute("UPDATE review_cards SET delivery='changed'")
                    checks.append(self.total_changes)
                return super().execute(statement, parameters)

        def tracked_connect(*args, **kwargs):
            return original(*args, factory=GuardedConnection, **kwargs)

        try:
            self.assertEqual(writer.execute('PRAGMA journal_mode=WAL').fetchone()[0], 'wal')
            # Leave a committed change in the active writer's WAL, so an
            # immutable/stale read would miss this pending card.
            writer.execute('INSERT INTO review_cards VALUES(?,?,?,?)',
                           ('身份：D=张小翠\n生产日：2027-01-03\n冰冰袜：4双',
                            'sent', 'pending', 'production'))
            writer.commit()
            self.assertTrue(Path(str(self.db) + '-wal').exists())
            with patch.object(missing_alerts.sqlite3, 'connect', side_effect=tracked_connect):
                rows = missing_alerts._review_status_rows(str(self.db))
            self.assertTrue(any('2027-01-03' in summary for summary, _ in rows))
            self.assertEqual(checks, [0])
            self.assertEqual(writer.execute(
                "SELECT COUNT(*) FROM review_cards WHERE delivery='changed'"
            ).fetchone()[0], 0)
        finally:
            writer.close()

    def test_missing_database_is_not_created_or_rendered_as_an_empty_queue(self):
        absent = Path(self.temporary.name) / 'not-created.sqlite'
        with patch.object(missing_alerts.time, 'sleep'):
            with self.assertRaisesRegex(RuntimeError, '核对卡状态读取失败'):
                missing_alerts.review_status_section(str(absent), 2027)
        self.assertFalse(absent.exists())

    def test_open_failure_is_bounded_and_recovers_same_snapshot(self):
        connection = MagicMock()
        connection.execute.return_value.fetchall.return_value = [('summary', 'sent')]
        failure = sqlite3.OperationalError('unable to open database file')
        with patch.object(missing_alerts.sqlite3, 'connect',
                          side_effect=[failure, failure, connection]) as connect, \
             patch.object(missing_alerts.time, 'sleep') as sleep:
            rows = missing_alerts._review_status_rows(str(self.db))
        self.assertEqual(rows, [('summary', 'sent')])
        self.assertEqual(connect.call_count, 3)
        self.assertEqual(connect.call_args,
                         call(self.db.resolve().as_uri() + '?mode=rw', uri=True, timeout=20))
        self.assertEqual(sleep.call_args_list, [call(.2), call(.5)])
        connection.close.assert_called_once_with()

    def test_query_lock_closes_connection_before_retry(self):
        events = []
        first, second = MagicMock(), MagicMock()
        first.execute.side_effect = sqlite3.OperationalError('database is locked')
        first.close.side_effect = lambda: events.append('closed-first')
        second.execute.return_value.fetchall.return_value = []
        second.close.side_effect = lambda: events.append('closed-second')
        with patch.object(missing_alerts.sqlite3, 'connect', side_effect=[first, second]), \
             patch.object(missing_alerts.time, 'sleep', side_effect=lambda _: events.append('sleep')):
            self.assertEqual(missing_alerts._review_status_rows(str(self.db)), [])
        self.assertEqual(events, ['closed-first', 'sleep', 'closed-second'])

    def test_permanent_error_does_not_retry_or_hide_queue(self):
        connection = MagicMock()
        connection.execute.side_effect = sqlite3.OperationalError('no such table: review_cards')
        with patch.object(missing_alerts.sqlite3, 'connect', return_value=connection) as connect, \
             patch.object(missing_alerts.time, 'sleep') as sleep:
            with self.assertRaisesRegex(RuntimeError, '核对卡状态读取失败.*no such table'):
                missing_alerts.review_status_section(str(self.db), 2027)
        connect.assert_called_once()
        sleep.assert_not_called()
        connection.close.assert_called_once_with()

    def test_exhausted_open_failures_raise_instead_of_empty_card_state(self):
        with patch.object(missing_alerts.sqlite3, 'connect',
                          side_effect=sqlite3.OperationalError('unable to open database file')) as connect, \
             patch.object(missing_alerts.time, 'sleep'):
            with self.assertRaisesRegex(RuntimeError, '核对卡状态读取失败') as error:
                missing_alerts.review_status_section(str(self.db), 2027)
        self.assertEqual(connect.call_count, 3)
        self.assertIsInstance(error.exception.__cause__, sqlite3.OperationalError)

    def test_failed_review_read_prevents_status_publish(self):
        import commit_guard
        import deterministic_upload

        self.db.with_name(missing_alerts.STATUS_SNAPSHOT_NAME).write_text(
            json.dumps({'pending': []}), encoding='utf-8'
        )
        remote = {'sha': 'fixture-sha',
                  'content': base64.b64encode(b'fixture ledger').decode()}
        with patch.object(missing_alerts, '_platform_status', return_value={}), \
             patch.object(commit_guard, 'gh_read_json', return_value=remote), \
             patch.object(deterministic_upload, '_update_coverage',
                          return_value='fixture candidate\n### 异常检查\n'), \
             patch.object(missing_alerts, '_review_status_rows',
                          side_effect=RuntimeError('核对卡状态读取失败')), \
             patch.object(missing_alerts.subprocess, 'run') as publish:
            results = missing_alerts.sync_pending_queue_to_github(str(self.db))
        self.assertEqual({item['year'] for item in results}, {'2026', '2027'})
        self.assertTrue(all(item['status'] == 'failed' for item in results))
        self.assertTrue(all('核对卡状态读取失败' in item['error'] for item in results))
        publish.assert_not_called()


if __name__ == '__main__':
    unittest.main()
