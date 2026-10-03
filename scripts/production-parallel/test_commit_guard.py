import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import commit_guard
import queue_store as q
import review_cards


def row(content, kind, production_date=None):
    extracted = {'kind': kind}
    if production_date is not None:
        extracted['production_date'] = production_date
    return {
        'event': json.dumps({'content': content}, ensure_ascii=False),
        'result': json.dumps({'extracted': extracted}, ensure_ascii=False),
    }


class BatchBlockingTests(unittest.TestCase):
    def test_later_different_production_day_does_not_block_current_batch(self):
        later_report = row('棉堆堆袜：5000', 'report', '2026-09-23')
        self.assertFalse(commit_guard.blocks_batch(later_report, '2026-09-22'))

    def test_same_day_report_still_blocks_current_batch(self):
        same_day = row('更正：棉堆堆袜改为5000', 'correction', '2026-09-22')
        self.assertTrue(commit_guard.blocks_batch(same_day, '2026-09-22'))

    def test_confirmation_never_blocks_current_batch(self):
        confirmation = row('确认上传', 'confirmation')
        self.assertFalse(commit_guard.blocks_batch(confirmation, '2026-09-22'))


class GithubReadTests(unittest.TestCase):
    def test_transient_read_retries_then_succeeds(self):
        calls = []
        responses = [
            commit_guard.subprocess.TimeoutExpired('gh', 15),
            commit_guard.subprocess.CalledProcessError(1, 'gh', output='unexpected EOF'),
            b'{"sha":"abc","content":""}',
        ]

        def fake_check_output(*args, **kwargs):
            calls.append((args, kwargs))
            result = responses.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        with mock.patch.object(commit_guard.subprocess, 'check_output', side_effect=fake_check_output), \
             mock.patch.object(commit_guard.time, 'sleep') as sleep:
            value = commit_guard.gh_read_json('repos/example/file')
        self.assertEqual(value['sha'], 'abc')
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(calls[0][1]['timeout'], commit_guard.GH_READ_TIMEOUT)

    def test_permanent_read_error_does_not_retry(self):
        error = commit_guard.subprocess.CalledProcessError(1, 'gh', output='HTTP 404 not found')
        with mock.patch.object(commit_guard.subprocess, 'check_output', side_effect=error) as run, \
             mock.patch.object(commit_guard.time, 'sleep') as sleep:
            with self.assertRaises(commit_guard.subprocess.CalledProcessError):
                commit_guard.gh_read_json('repos/example/missing')
        self.assertEqual(run.call_count, 1)
        sleep.assert_not_called()


if __name__ == '__main__':
    unittest.main()
