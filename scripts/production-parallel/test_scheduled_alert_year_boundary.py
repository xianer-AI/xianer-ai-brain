"""Reminder recovery across years; all platform I/O and state are isolated."""
import datetime as dt
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

import commit_guard
import missing_alerts
import queue_store as queue


def ledger(worker, day):
    identifier = day.replace('-', '') + '-' + worker + '-001'
    return (f'## {worker}｜隔离测试员工\n'
            f'| {identifier} | 棉堆堆袜 | 1 | 已确认 |\n')


class ScheduledAlertYearBoundaryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = str(Path(directory.name) / 'inbox.sqlite')
        missing_alerts._ensure_delivery_table(self.db)
        self.posts = []
        self.reads = []
        self.transport = types.SimpleNamespace(request=self.request)
        for mock in (
            patch.dict(os.environ, {'PRODUCTION_LEDGER_ENDPOINT': ''}),
            patch.dict(sys.modules, {'transport': self.transport}),
            patch('backfill_flow.register_verification'),
        ):
            mock.start()
            self.addCleanup(mock.stop)

    def request(self, _actor, method, path, body=None):
        if method == 'GET':
            return {'data': {'items': [{'deleted': False}]}}
        self.assertEqual((method, path),
                         ('POST', '/im/v1/messages?receive_id_type=chat_id'))
        self.posts.append(body)
        return {'data': {'message_id': f'om_isolated_alert_{len(self.posts)}'}}

    def now(self, day):
        return dt.datetime.fromisoformat(day + 'T12:00:00').replace(
            tzinfo=ZoneInfo('Asia/Shanghai'))

    def snapshot(self, snapshots):
        def read(endpoint):
            self.reads.append(endpoint)
            return snapshots[endpoint]
        return patch.object(missing_alerts, '_ledger_snapshot', side_effect=read)

    def test_january_first_checks_previous_years_closed_production_day(self):
        for year in (2027, 2028):
            with self.subTest(year=year), tempfile.TemporaryDirectory() as directory:
                db = str(Path(directory) / 'inbox.sqlite')
                self.posts.clear()
                self.reads.clear()
                endpoint = commit_guard.LEDGER_ENDPOINTS[year - 1]
                with self.snapshot({endpoint: ledger('A', f'{year - 1}-12-30')}):
                    delivered = missing_alerts.deliver_scheduled_alerts(
                        db, now=self.now(f'{year}-01-01'))
                self.assertEqual(self.reads, [endpoint])
                self.assertEqual(delivered, [f'A:{year - 1}-12-31'])
                self.assertEqual(len(self.posts), 1)
                payload = json.loads(self.posts[0]['content'])
                self.assertIn(f'{year - 1}-12-31',
                              payload['elements'][0]['text']['content'])

    def test_old_failed_queue_recovers_without_new_year_alerts_and_no_duplicate(self):
        with queue.conn(self.db) as conn:
            conn.execute(
                'INSERT INTO scheduled_missing_alerts '
                '(alert_key,worker,production_date,message,state,created,delivery_uuid) '
                'VALUES(?,?,?,?,?,?,?)',
                ('A:2026-12-31', 'A', '2026-12-31', 'isolated',
                 'failed', 0, 'isolated_stable_uuid'))
        endpoint = commit_guard.LEDGER_ENDPOINTS[2027]
        previous_endpoint = commit_guard.LEDGER_ENDPOINTS[2026]
        with self.snapshot({endpoint: '', previous_endpoint: ''}):
            first = missing_alerts.deliver_scheduled_alerts(
                self.db, now=self.now('2027-01-02'))
            second = missing_alerts.deliver_scheduled_alerts(
                self.db, now=self.now('2027-01-02') + dt.timedelta(minutes=1))
        self.assertEqual(self.reads, [endpoint, previous_endpoint,
                                     endpoint, previous_endpoint])
        self.assertEqual(first, ['A:2026-12-31'])
        self.assertEqual(second, [])
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.posts[0]['uuid'], 'isolated_stable_uuid')
        payload = json.loads(self.posts[0]['content'])
        self.assertIn('2026-12-31', payload['elements'][0]['text']['content'])
        with queue.conn(self.db) as conn:
            state = conn.execute(
                'SELECT state FROM scheduled_missing_alerts WHERE alert_key=?',
                ('A:2026-12-31',)).fetchone()['state']
        self.assertEqual(state, 'sent')

    def test_current_year_closed_day_still_reads_and_reminds_same_year(self):
        endpoint = commit_guard.LEDGER_ENDPOINTS[2027]
        with self.snapshot({endpoint: ledger('C', '2027-02-01')}):
            delivered = missing_alerts.deliver_scheduled_alerts(
                self.db, now=self.now('2027-02-03'))
        self.assertEqual(self.reads, [endpoint])
        self.assertEqual(delivered, ['C:2027-02-02'])
        self.assertEqual(len(self.posts), 1)

    def test_empty_ledger_without_pending_queue_does_not_send_any_card(self):
        endpoint = commit_guard.LEDGER_ENDPOINTS[2027]
        with self.snapshot({endpoint: ''}):
            delivered = missing_alerts.deliver_scheduled_alerts(
                self.db, now=self.now('2027-01-02'))
        self.assertEqual(delivered, [])
        self.assertEqual(self.posts, [])

    def test_unavailable_authoritative_read_still_sends_nothing(self):
        with patch.object(missing_alerts, '_ledger_snapshot',
                          side_effect=RuntimeError('isolated read failure')):
            delivered = missing_alerts.deliver_scheduled_alerts(
                self.db, now=self.now('2027-01-02'))
        self.assertEqual(delivered, [])
        self.assertEqual(self.posts, [])

    def test_revoked_detail_never_counts_as_an_effective_date(self):
        text = ledger('A', '2027-01-01')
        for status in ('撤销', '作废', '已确认后撤销'):
            with self.subTest(status=status):
                dates = missing_alerts.date_map_from_ledger(text.replace('已确认', status))
                self.assertEqual(dates['A'], set())

    def test_resolved_old_failed_queue_is_suppressed_using_its_own_year(self):
        for state in ('pending', 'failed'):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as directory:
                db = str(Path(directory) / 'inbox.sqlite')
                missing_alerts._ensure_delivery_table(db)
                with queue.conn(db) as conn:
                    conn.execute(
                        'INSERT INTO scheduled_missing_alerts '
                        '(alert_key,worker,production_date,message,state,created) VALUES(?,?,?,?,?,?)',
                        ('A:2026-12-31', 'A', '2026-12-31', 'isolated', state, 0))
                with self.snapshot({commit_guard.LEDGER_ENDPOINTS[2027]: '',
                                    commit_guard.LEDGER_ENDPOINTS[2026]: ledger('A', '2026-12-31')}):
                    delivered = missing_alerts.deliver_scheduled_alerts(
                        db, now=self.now('2027-01-02'))
                self.assertEqual(delivered, [])
                self.assertEqual(self.posts, [])
                with queue.conn(db) as conn:
                    result = conn.execute('SELECT state FROM scheduled_missing_alerts').fetchone()
                self.assertEqual(result['state'], 'ignored')

    def test_ignored_and_completed_alerts_cannot_be_revived(self):
        with queue.conn(self.db) as conn:
            for day, state in [('2027-01-02', 'ignored'), ('2027-01-03', 'completed')]:
                conn.execute(
                    'INSERT INTO scheduled_missing_alerts '
                    '(alert_key,worker,production_date,message,state,created) VALUES(?,?,?,?,?,?)',
                    ('A:' + day, 'A', day, 'isolated', state, 0))
        endpoint = commit_guard.LEDGER_ENDPOINTS[2027]
        with self.snapshot({endpoint: ledger('A', '2027-01-01')}):
            delivered = missing_alerts.deliver_scheduled_alerts(
                self.db, now=self.now('2027-01-04'))
        self.assertEqual(delivered, [])
        self.assertEqual(self.posts, [])
        with queue.conn(self.db) as conn:
            self.assertEqual([row['state'] for row in conn.execute(
                'SELECT state FROM scheduled_missing_alerts ORDER BY production_date')],
                ['ignored', 'completed'])

    def test_filled_sent_day_without_success_receipt_keeps_next_day_locked(self):
        with queue.conn(self.db) as conn:
            conn.execute(
                'INSERT INTO scheduled_missing_alerts '
                '(alert_key,worker,production_date,message,state,message_id,created) '
                'VALUES(?,?,?,?,?,?,?)',
                ('A:2027-01-01', 'A', '2027-01-01', 'isolated',
                 'sent', 'om_isolated_card', 0))
        with self.snapshot({commit_guard.LEDGER_ENDPOINTS[2027]: ledger('A', '2027-01-01')}):
            delivered = missing_alerts.deliver_scheduled_alerts(
                self.db, now=self.now('2027-01-03'))
        self.assertEqual(delivered, [])
        self.assertEqual(self.posts, [])
        with queue.conn(self.db) as conn:
            states = [row['state'] for row in conn.execute(
                'SELECT state FROM scheduled_missing_alerts ORDER BY production_date')]
        self.assertEqual(states, ['sent', 'pending'])

    def test_revoked_source_with_an_ignored_alert_stays_ignored(self):
        with queue.conn(self.db) as conn:
            conn.execute(
                'INSERT INTO scheduled_missing_alerts '
                '(alert_key,worker,production_date,message,state,created) VALUES(?,?,?,?,?,?)',
                ('A:2027-01-02', 'A', '2027-01-02', 'isolated', 'ignored', 0))
        text = ledger('A', '2027-01-01') + ledger('A', '2027-01-02').split('\n', 1)[1].replace('已确认', '撤销')
        with self.snapshot({commit_guard.LEDGER_ENDPOINTS[2027]: text}):
            delivered = missing_alerts.deliver_scheduled_alerts(
                self.db, now=self.now('2027-01-03'))
        self.assertEqual(delivered, [])
        self.assertEqual(self.posts, [])
        with queue.conn(self.db) as conn:
            self.assertEqual(conn.execute('SELECT state FROM scheduled_missing_alerts').fetchone()['state'],
                             'ignored')
    def test_persisted_pending_reminder_does_not_send_before_next_day_noon(self):
        with queue.conn(self.db) as conn:
            conn.execute(
                'INSERT INTO scheduled_missing_alerts '
                '(alert_key,worker,production_date,message,state,created) VALUES(?,?,?,?,?,?)',
                ('A:2027-01-01', 'A', '2027-01-01', 'isolated', 'pending', 0))
        endpoint = commit_guard.LEDGER_ENDPOINTS[2027]
        before_due = self.now('2027-01-02') - dt.timedelta(seconds=1)
        with self.snapshot({endpoint: ''}):
            delivered = missing_alerts.deliver_scheduled_alerts(self.db, now=before_due)
        self.assertEqual(delivered, [])
        self.assertEqual(self.posts, [])

    def test_unreadable_old_year_keeps_its_pending_queue_unsent(self):
        with queue.conn(self.db) as conn:
            conn.execute(
                'INSERT INTO scheduled_missing_alerts '
                '(alert_key,worker,production_date,message,state,created) VALUES(?,?,?,?,?,?)',
                ('A:2026-12-31', 'A', '2026-12-31', 'isolated', 'failed', 0))
        # The missing old-year entry deliberately fails the mocked remote read.
        with self.snapshot({commit_guard.LEDGER_ENDPOINTS[2027]: ''}):
            delivered = missing_alerts.deliver_scheduled_alerts(
                self.db, now=self.now('2027-01-02'))
        self.assertEqual(delivered, [])
        self.assertEqual(self.posts, [])

    def test_confirmed_attendance_day_never_creates_or_recovers_a_reminder(self):
        text = ledger('A', '2027-01-01') + (
            '## 人员生产记录覆盖情况\n### 已确认出勤状态日期\n'
            '| A｜徐超超 | 2027年1月2日 | 已确认未上班（不计入生产统计） |\n')
        with queue.conn(self.db) as conn:
            conn.execute(
                'INSERT INTO scheduled_missing_alerts '
                '(alert_key,worker,production_date,message,state,created) VALUES(?,?,?,?,?,?)',
                ('A:2027-01-02', 'A', '2027-01-02', 'isolated', 'failed', 0))
        with self.snapshot({commit_guard.LEDGER_ENDPOINTS[2027]: text}):
            delivered = missing_alerts.deliver_scheduled_alerts(
                self.db, now=self.now('2027-01-03'))
        self.assertEqual(delivered, [])
        self.assertEqual(self.posts, [])


if __name__ == '__main__':
    unittest.main()
