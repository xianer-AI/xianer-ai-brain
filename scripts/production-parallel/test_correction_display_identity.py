"""Authenticated display additions must preserve durable production bindings."""
import json
from pathlib import Path
import socket
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import backfill_flow
import card_builder
import quantity_display
import queue_store as q
import review_cards
import worker_identity


class CorrectionDisplayIdentityTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = str(Path(directory.name) / 'isolated.sqlite')
        review_cards.init(self.db)
        worker_identity.init(self.db)
        self.sequence = 0
        with q.conn(self.db) as conn:
            for worker, (name, _) in backfill_flow.WORKERS.items():
                conn.execute('INSERT INTO worker_identity VALUES(?,?,?,?,?,?)',
                             (worker, name, f'ou_isolated_{worker}', 'fixture', 'fixture', 0))
        for target, name in ((socket.socket, 'connect'), (subprocess, 'run'),
                             (subprocess, 'check_output'), (subprocess, 'Popen')):
            guard = patch.object(target, name, side_effect=AssertionError('live I/O forbidden'))
            guard.start()
            self.addCleanup(guard.stop)

    def card(self, worker='A', year=2026, status=False):
        self.sequence += 1
        source = f'om_isolated_{self.sequence}'
        name, process = backfill_flow.WORKERS[worker]
        day = f'{year}-10-06'
        text = '\n'.join([f'{worker}={name}', f'生产日期：{day}', f'工序：{process}',
                          *[f'{product}：100' for product in card_builder._PRODUCTS]])
        q.put(self.db, {'messageId': source, 'senderId': f'ou_isolated_{worker}',
                       'groupId': review_cards.GROUP, 'content': text})
        extracted = {'kind': 'report', 'worker': worker, 'production_date': day,
                     'items': [{'product': product, 'process': process, 'quantity': 100}
                               for product in card_builder._PRODUCTS]}
        if status:
            extracted.update(status_only=True, attendance_status='not_worked')
        with q.conn(self.db) as conn:
            conn.execute("UPDATE inbox SET status='ready',result=? WHERE id=?",
                         (json.dumps({'extracted': extracted}), source))
        return review_cards.issue(self.db, source, text + '\n合计：600双')

    def stored(self, token):
        with q.conn(self.db) as conn:
            return dict(conn.execute('SELECT * FROM review_cards WHERE token=?', (token,)).fetchone())

    def test_both_years_all_workers_modify_note_is_response_only_and_repeatable(self):
        for year in (2026, 2027):
            for worker in backfill_flow.WORKERS:
                with self.subTest(year=year, worker=worker):
                    card = self.card(worker, year)
                    before = self.stored(card['token'])
                    source_before = q.get(self.db, card['source'])
                    callback = f'card-action-isolated-{self.sequence}'
                    args = (self.db, card['token'], 'modify', f'ou_isolated_{worker}',
                            review_cards.GROUP, callback)
                    result = review_cards.act(*args)
                    self.assertEqual(result['quantity_note'], quantity_display.quantity_note(worker))
                    after = self.stored(card['token'])
                    self.assertEqual(after['state'], 'modified')
                    for key in ('source', 'token', 'summary', 'digest', 'sender', 'grp'):
                        self.assertEqual(after[key], before[key])
                    durable_result = json.loads(after['result'])
                    self.assertNotIn('quantity_note', durable_result)
                    self.assertEqual(durable_result,
                                     {k: v for k, v in result.items() if k != 'quantity_note'})
                    self.assertEqual(q.get(self.db, card['source']), source_before)
                    self.assertEqual(review_cards.act(*args), result)
                    self.assertEqual(self.stored(card['token']), after)
        with q.conn(self.db) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM confirmation_receipts').fetchone()[0], 0)

    def test_ambiguous_unknown_and_status_cards_do_not_receive_quantity_labels(self):
        for summary in ('身份：待核实', '身份：A=徐超超\n身份：C=李鸿玉'):
            card = self.card()
            with q.conn(self.db) as conn:
                conn.execute('UPDATE review_cards SET summary=? WHERE token=?', (summary, card['token']))
            result = review_cards.act(self.db, card['token'], 'modify', 'ou_isolated_A',
                                      review_cards.GROUP, f'card-action-unknown-{self.sequence}')
            self.assertNotIn('quantity_note', result)
        card = self.card(status=True)
        result = review_cards.act(self.db, card['token'], 'modify', 'ou_isolated_A',
                                  review_cards.GROUP, f'card-action-status-{self.sequence}')
        self.assertNotIn('quantity_note', result)

    def test_unauthorized_action_rejected_without_disclosing_or_changing_the_card(self):
        card = self.card()
        before = self.stored(card['token'])
        with self.assertRaisesRegex(ValueError, '不属于你'):
            review_cards.act(self.db, card['token'], 'modify', 'ou_isolated_B',
                             review_cards.GROUP, 'card-action-forged')
        self.assertEqual(self.stored(card['token']), before)

    def test_confirm_and_defer_keep_original_return_and_persistence_contract(self):
        for action in ('confirm', 'defer'):
            card = self.card('C')
            result = review_cards.act(self.db, card['token'], action, 'ou_isolated_C',
                                      review_cards.GROUP, f'card-action-{action}')
            self.assertEqual(set(result), {'source', 'token', 'action', 'callback'})
            self.assertEqual(json.loads(self.stored(card['token'])['result']), result)

    def test_mismatched_template_names_target_for_all_workers_and_writes_no_batch(self):
        for year in (2026, 2027):
            for worker, (name, process) in backfill_flow.WORKERS.items():
                with self.subTest(year=year, worker=worker):
                    text = '\n'.join(['补报', f'{worker}=不可信输入姓名',
                                      f'生产日期：{year}-10-06', f'工序：{process}',
                                      *[f'{product}：100' for product in card_builder._PRODUCTS]])
                    with patch.object(review_cards, 'deliver_card') as deliver:
                        result = backfill_flow.handle_template('om_wrong_sender', 'ou_other', text, db=self.db)
                    self.assertIn(f'与{name}的已核实账号不一致', result['text'])
                    self.assertNotIn('不可信输入姓名', result['text'])
                    self.assertNotIn('message_id', result)
                    deliver.assert_not_called()
        with q.conn(self.db) as conn:
            for table in ('inbox', 'review_cards', 'confirmation_receipts'):
                self.assertEqual(conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
