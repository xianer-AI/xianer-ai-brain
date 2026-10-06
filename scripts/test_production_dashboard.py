"""Isolated dashboard release and served-snapshot verification regressions."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import build_production_dashboard as build


VERSION = {'workbench_version': 'V1.18', 'sync_protocol_version': 'S1',
           'card_protocol_version': 'CARD-INTERACTIVE-1', 'release_id': '2026-10-06-v1.18',
           'release_status': '正式'}
PUBLISHER = Path(__file__).with_name('publish_production_dashboard.py')


class SnapshotTests(unittest.TestCase):
    def render(self, ledger='原台账', version=None, generated_at='2026-10-06T15:00:00+00:00'):
        return build.render_snapshot('<main>原布局</main><script>run();</script>', ledger,
                                     version or VERSION, generated_at=generated_at, builder_hash='a' * 64)

    def test_release_uses_formal_version_and_exact_ledger(self):
        html, manifest, release = self.render()
        self.assertEqual(json.loads(manifest)['rules_release_id'], VERSION['release_id'])
        self.assertEqual(release['workbench_version'], 'V1.18')
        self.assertEqual(release['ledger_sha256'], build.sha256('原台账'.encode()))
        self.assertIn('window.__DASHBOARD_RELEASE__=', html.decode())
        self.assertIn('<main>原布局</main>', html.decode())

    def test_rebuild_time_alone_does_not_trigger_publish(self):
        first = self.render(generated_at='2026-10-06T15:00:00+00:00')[2]
        second = self.render(generated_at='2026-10-06T16:00:00+00:00')[2]
        self.assertEqual(first['source_digest'], second['source_digest'])

    def test_ledger_or_rule_change_changes_source_digest(self):
        first = self.render()[2]['source_digest']
        self.assertNotEqual(first, self.render(ledger='最新台账')[2]['source_digest'])
        changed = dict(VERSION, release_id='2026-10-07-v1.18')
        self.assertNotEqual(first, self.render(version=changed)[2]['source_digest'])

    def test_incomplete_release_or_bad_template_is_rejected(self):
        with self.assertRaises(ValueError):
            self.render(version={'workbench_version': 'V1.18'})
        with self.assertRaises(ValueError):
            build.render_snapshot('no script', '台账', VERSION)

    def test_ledger_cannot_close_the_data_script(self):
        html = self.render(ledger='</script><script>alert(1)</script>')[0].decode()
        self.assertEqual(html.count('<script>'), 1)
        self.assertIn('\\u003c/script>', html)


class PublisherTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location('dashboard_publisher', PUBLISHER)
        cls.publisher = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.publisher)

    def test_public_readback_checks_both_real_files(self):
        module = self.publisher
        class Response:
            status = 200
            def __init__(self, body): self.body = body
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return self.body
        with patch.object(module.urllib.request, 'urlopen', side_effect=[Response(b'html'), Response(b'manifest')]) as opener:
            module.verify_public({'index.html': build.sha256(b'html'), 'release.json': build.sha256(b'manifest')})
        urls = [call.args[0].full_url for call in opener.call_args_list]
        self.assertIn('/index.html?_verify=', urls[0])
        self.assertIn('/release.json?_verify=', urls[1])

    def test_stale_online_html_is_not_success(self):
        class Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return b'old html'
        with patch.object(self.publisher.urllib.request, 'urlopen', return_value=Response()):
            with self.assertRaisesRegex(RuntimeError, 'differs'):
                self.publisher.verify_public({'index.html': build.sha256(b'new html')})

    def test_failed_publish_does_not_advance_success_state(self):
        module = self.publisher
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            state = directory / 'state.json'
            state.write_text('{"digest":"old"}')
            executable = directory / 'tcb'; executable.touch()
            snapshot = build.render_snapshot('<script>run();</script>', '台账', VERSION)
            with patch.multiple(module, STATE=state, LOCK=directory / 'lock', LOG=directory / 'log', TCB=executable), \
                    patch.object(module, 'capture_snapshot', return_value=snapshot), \
                    patch.object(module.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, 'ok', '')), \
                    patch.object(module, 'verify_public', side_effect=RuntimeError('still stale')), \
                    patch.object(module.time, 'sleep'):
                self.assertEqual(module.main(), 1)
            self.assertEqual(json.loads(state.read_text())['digest'], 'old')


class BrowserLogicTests(unittest.TestCase):
    def run_node(self, code):
        executable = shutil.which('node') or '/Users/xianer/.local/share/fnm/node-versions/v24.21.0/installation/bin/node'
        result = subprocess.run([executable, '-e', code], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_sync_banner_requires_evidence_and_matching_versions(self):
        template = build.TEMPLATE.read_text()
        pure = template.split('function snapshotSyncState', 1)[1].split('function renderSyncBanner', 1)[0]
        code = 'const assert=require("node:assert/strict");function snapshotSyncState' + pure
        code += '''
const release={workbench_version:'V1.18',rules_release_id:'2026-10-06-v1.18',source_digest:'a'.repeat(64),release_status:'正式'};
const rows=['GitHub','OpenClaw','飞书'].map(source=>({source,version:'V1.18 / S1',status:'已同步'}));
const checks='| 三端规则版本 | 一致 |';
assert.equal(snapshotSyncState(release,rows,checks).kind,'ok');
assert.equal(snapshotSyncState(release,[],checks).kind,'warn');
assert.equal(snapshotSyncState(release,rows.map(x=>({...x,version:'V1.17'})),checks).kind,'error');
assert.equal(snapshotSyncState(release,rows.map(x=>({...x,status:'上传失败'})),checks).kind,'error');
assert.equal(snapshotSyncState(release,rows.map(x=>({...x,status:'上传处理中'})),checks).kind,'warn');
assert.equal(snapshotSyncState({...release,release_status:'测试'},rows,checks).kind,'warn');
'''
        self.run_node(code)

    def test_browser_no_cache_check_update_and_network_failure(self):
        template = build.TEMPLATE.read_text()
        functions = 'function cacheBypassUrl' + template.split('function cacheBypassUrl', 1)[1].split('function renderAlerts', 1)[0]
        code = '''
const assert=require('node:assert/strict');
let websiteCheckInFlight=false,websiteVerification='checking',websiteCheckedAt=null;
const dashboardRelease={source_digest:'a'.repeat(64)};
const location={href:'https://example.test/production-dashboard/index.html?old=1',replace(value){this.replacement=value;}};
const window={scrollY:120};const saveView=()=>{};const renderSyncBanner=()=>{};
let latest={schema:1,source_digest:'a'.repeat(64),rules_release_id:'2026-10-06-v1.18'};
let fetch=async(url,options)=>{assert.equal(options.cache,'no-store');assert.match(url,/release.json.*_sync=/);return {ok:true,json:async()=>latest};};
'''+functions+'''
(async()=>{
await checkPublishedSnapshot();assert.equal(websiteVerification,'verified');assert.ok(websiteCheckedAt);assert.equal(location.replacement,undefined);
latest={...latest,source_digest:'b'.repeat(64)};await checkPublishedSnapshot();assert.match(location.replacement,/index.html.*_sync=/);
location.replacement=undefined;fetch=async()=>{throw Error('offline');};await checkPublishedSnapshot();assert.equal(websiteVerification,'failed');assert.equal(location.replacement,undefined);
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        self.run_node(code)


if __name__ == '__main__':
    unittest.main()
