"""Backfill callbacks require durable sender IDs; no live messages are sent."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import backfill_flow
import queue_store as queue
import review_cards
import worker_identity


class BackfillChoiceIdentityTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = Path(directory.name) / 'flow.sqlite'
        backfill_flow.init(self.db)
        queue.init(str(self.db))
        worker_identity.init(str(self.db))
        with queue.conn(str(self.db)) as conn:
            for worker, sender in [('A', 'ou_isolated_A'), ('B', 'ou_isolated_B')]:
                conn.execute('INSERT INTO worker_identity VALUES(?,?,?,?,?,?)',
                             (worker, worker_identity.NAMES[worker], sender,
                              review_cards.OWNER, 'isolated verified account', 0))
        backfill_flow.register_verification('A', '2027-01-01', 'om_isolated_A_card', db=self.db)
        send_patch = patch.object(backfill_flow, '_send', return_value='om_isolated_template')
        self.send = send_patch.start()
        self.addCleanup(send_patch.stop)
        for mock in (patch.object(backfill_flow, '_message_is_deleted', return_value=False),
                     patch.object(backfill_flow, '_parent_message', return_value=None)):
            mock.start()
            self.addCleanup(mock.stop)

    def choice(self, sender, text='生产补报 3 A 2027-01-01'):
        return backfill_flow.handle_choice('om_isolated_callback', sender, text, db=self.db)

    def state(self):
        with queue.conn(str(self.db)) as conn:
            return conn.execute('SELECT status FROM requests WHERE worker="A"').fetchone()[0]

    def test_another_employee_cannot_send_or_reuse_targets_template(self):
        for state in ('verification_sent', 'template_sent'):
            with self.subTest(state=state):
                with queue.conn(str(self.db)) as conn:
                    conn.execute('UPDATE requests SET status=?,template_message_id=?',
                                 (state, 'om_existing' if state == 'template_sent' else None))
                result = self.choice('ou_isolated_B')
                self.assertTrue(result['handled'])
                self.assertIn('账号', result['text'])
                self.assertNotIn('message_id', result)
                self.assertEqual(self.state(), state)
                self.send.assert_not_called()

    def test_unknown_sender_cannot_claim_identity_through_callback_text(self):
        result = self.choice('ou_unbound_徐超超')
        self.assertIn('账号', result['text'])
        self.assertEqual(self.state(), 'verification_sent')
        self.send.assert_not_called()

    def test_verified_employee_duplicate_click_sends_exactly_one_template(self):
        first = self.choice('ou_isolated_A')
        second = self.choice('ou_isolated_A')
        self.assertEqual(first['message_id'], 'om_isolated_template')
        self.assertTrue(second['handled'])
        self.assertEqual(self.state(), 'template_sent')
        self.assertEqual(self.send.call_count, 1)

    def test_verified_owner_can_proxy_for_the_bound_employee(self):
        result = self.choice(review_cards.OWNER)
        self.assertEqual(result['message_id'], 'om_isolated_template')
        self.assertEqual(self.send.call_count, 1)
        content = self.send.call_args.args[0]['elements'][0]['content']
        self.assertIn('A=徐超超', content)
        self.assertIn('2027-01-01', content)

    def test_unbound_target_must_be_verified_before_owner_can_send_template(self):
        with queue.conn(str(self.db)) as conn:
            conn.execute('DELETE FROM worker_identity WHERE worker="A"')
        result = self.choice(review_cards.OWNER)
        self.assertIn('账号', result['text'])
        self.assertEqual(self.state(), 'verification_sent')
        self.send.assert_not_called()

    def test_bare_button_fallback_cannot_select_another_employees_newest_card(self):
        result = self.choice('ou_isolated_B', '3 需要补报')
        self.assertIn('账号', result['text'])
        self.assertEqual(self.state(), 'verification_sent')
        self.send.assert_not_called()

    def test_superseded_request_cannot_generate_a_fresh_template(self):
        with queue.conn(str(self.db)) as conn:
            conn.execute('UPDATE requests SET status="superseded"')
        result = self.choice('ou_isolated_A')
        self.assertNotIn('message_id', result)
        self.assertEqual(self.state(), 'superseded')
        self.send.assert_not_called()


if __name__ == '__main__':
    unittest.main()
