import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
import subprocess
import commit_guard
import deterministic_upload as writer
import review_cards as cards
import queue_store as q

class RecoverySpeedTests(unittest.TestCase):
    def test_tls_read_recovers_without_worker_backoff(self):
        error = subprocess.CalledProcessError(1, 'gh', output='TLS handshake failed')
        with patch.object(commit_guard.subprocess, 'check_output', side_effect=[error, '{"sha":"ok"}']) as read, patch.object(commit_guard.time, 'sleep'):
            self.assertEqual(commit_guard.gh_read_json('fixture')['sha'], 'ok')
            self.assertEqual(read.call_count, 2)

    def test_conflict_rebuilds_but_ambiguous_write_does_not(self):
        with patch.object(writer, '_run_once', side_effect=[RuntimeError('远程已更新：fixture'), {'status':'verified'}]) as run:
            self.assertEqual(writer.run('fixture')['status'], 'verified')
            self.assertEqual(run.call_count, 2)
        with patch.object(writer, '_run_once', side_effect=RuntimeError('上传失败，保持待上传状态')) as run:
            with self.assertRaises(RuntimeError): writer.run('fixture')
            self.assertEqual(run.call_count, 1)

    def test_error_history_survives_success(self):
        with tempfile.TemporaryDirectory() as folder:
            db = str(Path(folder)/'fixture.sqlite')
            cards.init(db)
            with q.conn(db) as c:
                c.execute("INSERT INTO confirmation_receipts(message_id,sender,grp,text,source,token,status,attempts,next_at,created) VALUES('m','u','g','准确','s','t','dispatching',1,0,0)")
            detail = 'GitHub读取失败：TLS ' + 'detail ' * 100
            with patch.object(cards.time, 'time', return_value=100), patch.object(cards, '_verified_upload_receipt', return_value=False):
                cards.mark_confirmation_dispatch_failed(db, 'm', detail)
            with q.conn(db) as c:
                self.assertEqual(c.execute("SELECT next_at FROM confirmation_receipts").fetchone()[0],105)
            with patch.object(cards, 'verify_dispatch_receipt', return_value={}):
                cards.mark_confirmation_dispatched(db,'m','reply')
            with q.conn(db) as c:
                self.assertEqual(c.execute("SELECT detail FROM upload_attempt_history WHERE event='failed'").fetchone()[0],detail)
                self.assertEqual(c.execute("SELECT count(*) FROM upload_attempt_history WHERE event='succeeded'").fetchone()[0],1)

if __name__ == '__main__': unittest.main()
