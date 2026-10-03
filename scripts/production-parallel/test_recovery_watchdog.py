import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import recovery_watchdog
import review_cards


class RecoveryWatchdogTests(unittest.TestCase):
    def test_dead_dispatching_worker_is_requeued(self):
        with tempfile.TemporaryDirectory() as td:
            db = str(Path(td) / 'inbox.sqlite')
            review_cards.init(db)
            with review_cards.q.conn(db) as c:
                c.execute("""INSERT INTO confirmation_receipts
                    (message_id,sender,grp,text,source,token,status,error,attempts,next_at,created,worker_pid)
                    VALUES ('om_confirm','ou_worker','oc_group','准确','om_source','tok',
                            'dispatching',NULL,1,0,0,99999999)""")
            actions = recovery_watchdog.inspect(db, now=100, process_exists=lambda pid: False)
            self.assertEqual(actions[0]['action'], 'requeue_dispatch')
            self.assertEqual(actions[0]['message_id'], 'om_confirm')

    def test_verified_receipt_is_never_requeued_as_new_upload(self):
        action = recovery_watchdog.classify_receipt(
            {'message_id': 'om_confirm', 'source': 'om_source', 'status': 'dispatching', 'worker_pid': None},
            verified=True,
            process_exists=lambda pid: False,
        )
        self.assertEqual(action, 'reconcile_verified')

    def test_live_worker_is_left_alone(self):
        action = recovery_watchdog.classify_receipt(
            {'message_id': 'om_confirm', 'source': 'om_source', 'status': 'dispatching', 'worker_pid': 12},
            verified=False,
            process_exists=lambda pid: True,
        )
        self.assertEqual(action, 'wait')

    def test_reply_retry_action_does_not_collide_with_action_field(self):
        with tempfile.TemporaryDirectory() as td:
            db = str(Path(td) / 'inbox.sqlite')
            with patch.object(review_cards, 'retry_verified_replies', return_value=[
                    {'message_id': 'om_reply', 'action': 'reply_retry'}]):
                actions = recovery_watchdog.inspect(db, now=100,
                                                    process_exists=lambda pid: False)
            self.assertEqual(actions[0]['action'], 'reply_retry')
            self.assertEqual(actions[0]['watchdog_action'], 'retry_verified_reply')


if __name__ == '__main__':
    unittest.main()
