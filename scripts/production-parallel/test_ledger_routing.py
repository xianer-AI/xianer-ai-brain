"""Isolated route tests: all GitHub reads and writes are mocked."""
import base64
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import commit_guard as guard
import upload_task


class LedgerRoutingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.db = str(Path(temporary.name) / 'inbox.sqlite')
        self.source = 'om_source2027'
        self.confirmation = 'om_confirm2027'
        self.row = {'result': {'extracted': {'worker': 'B',
            'production_date': '2027-01-01', 'items': [
                {'product': '棉堆堆袜', 'quantity': 2, 'process': '下机'},
            ]}}}
        self.endpoint = guard.LEDGER_ENDPOINTS[2027]
        self.receipt_name = hashlib.sha256(self.source.encode()).hexdigest() + '.json'
        self.receipt_path = Path(self.db).parent / 'receipts' / self.receipt_name
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_inspect_result_exposes_year_target_to_legacy_worker(self):
        result = upload_task._result('task.json', {'source': self.source}, self.row,
                                     {}, {'status': 'pending'})
        self.assertEqual(result['ledger_endpoint'], self.endpoint)
        self.assertEqual(result['ledger_filename'], Path(self.endpoint).name)

    def test_fallback_commit_passes_task_target_to_guard(self):
        task = {'source': self.source, 'confirmation': self.confirmation,
                'ledger_endpoint': self.endpoint}
        with patch.object(upload_task, 'inspect', return_value=task), \
             patch.object(upload_task.subprocess, 'run') as run:
            upload_task.commit(self.db, 'task.json', 'sha', 'candidate.md', 'checked')
        self.assertEqual(run.call_args.kwargs['env']['PRODUCTION_LEDGER_ENDPOINT'],
                         self.endpoint)

    def test_direct_new_and_correction_writes_use_source_production_year(self):
        content = self.source + '\n' + self.confirmation
        encoded = base64.b64encode(content.encode()).decode()
        for require_absent in (True, False):
            with self.subTest(require_absent=require_absent), \
                 patch.object(guard, 'DB', self.db), \
                 patch.object(guard.q, 'get', return_value=self.row), \
                 patch.object(guard, 'gh_read_json', side_effect=[
                     {'sha': 'sha', 'content': ''}, {'sha': 'sha'}, {'content': encoded}
                 ]) as read, \
                 patch.object(guard, 'run_original_ledger_guard', return_value='ok'), \
                 patch.object(guard.subprocess, 'run', return_value=SimpleNamespace(
                     returncode=0, stdout=json.dumps({'commit': {'sha': 'commit'}}))) as write, \
                 contextlib.redirect_stdout(io.StringIO()):
                guard.write_remote(None, content, self.source, self.confirmation,
                                   'test', self.receipt_name, require_absent, 'sha', 'checked')
            self.assertTrue(all(call.args[0] == self.endpoint for call in read.call_args_list))
            self.assertIn(self.endpoint, write.call_args.args[0])
            receipt = json.loads(self.receipt_path.read_text())
            self.assertEqual(receipt['endpoint'], self.endpoint)
            self.assertIn('已更新：2027全年下机白胚半成品统计.md', receipt['message'])

    def test_pending_reread_uses_persisted_or_original_production_target(self):
        self.receipt_path.parent.mkdir()
        content = base64.b64encode((self.source + self.confirmation).encode()).decode()
        for endpoint in (None, self.endpoint):
            with self.subTest(persisted=bool(endpoint)):
                receipt = {'source': self.source, 'confirmation': self.confirmation,
                           'commit': 'commit', 'status': 'committed_pending_reread'}
                if endpoint:
                    receipt['endpoint'] = endpoint
                self.receipt_path.write_text(json.dumps(receipt))
                with patch.object(guard, 'DB', self.db), \
                     patch.object(guard.q, 'get', return_value=self.row), \
                     patch.object(guard, 'gh_read_json', return_value={'content': content}) as read, \
                     patch.object(guard.subprocess, 'check_output', return_value='commit'):
                    recovered = guard.recover_pending_receipt(self.source, self.confirmation)
                read.assert_called_once_with(self.endpoint)
                self.assertEqual(recovered['status'], 'verified')

    def test_environment_override_remains_available_for_isolated_migrations(self):
        endpoint = 'repos/test/contents/test-ledger.md'
        with patch.dict(os.environ, {'PRODUCTION_LEDGER_ENDPOINT': endpoint}):
            result = upload_task._result('task.json', {}, self.row, {}, {'status': 'pending'})
            self.assertEqual(result['ledger_endpoint'], endpoint)


if __name__ == '__main__':
    unittest.main()
