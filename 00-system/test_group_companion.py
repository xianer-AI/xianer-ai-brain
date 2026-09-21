"""Focused tests for durable chat delivery state transitions."""

import importlib.util
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest


COMPANION = Path("/Users/xianer/.openclaw/workspace/scripts/group-companion")
sys.path.insert(0, str(COMPANION))
spec = importlib.util.spec_from_file_location("chat_jobs_under_test", COMPANION / "chat_jobs.py")
chat_jobs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(chat_jobs)


class ChatJobsTests(unittest.TestCase):
    def test_expired_sending_job_becomes_uncertain_and_is_not_requeued(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "chat.sqlite"
            now = time.time()
            with chat_jobs.connect(db) as connection:
                connection.execute(
                    "INSERT INTO jobs(id,sender,event,actor,state,created,lease,expires,attempts) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    ("uncertain-1", "sender-1", "{}", "xiaowen", "sending", now - 400, "lease", now - 1, 1),
                )
            self.assertIsNone(chat_jobs.claim(db))
            with sqlite3.connect(db) as connection:
                state, lease, error = connection.execute(
                    "SELECT state,lease,error FROM jobs WHERE id='uncertain-1'"
                ).fetchone()
            self.assertEqual(state, "uncertain")
            self.assertIsNone(lease)
            self.assertEqual(error, "send_result_unknown")


if __name__ == "__main__":
    unittest.main()
