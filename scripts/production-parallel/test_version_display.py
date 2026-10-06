import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import coverage_tables
import deterministic_upload


class VersionDisplayTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.version = Path(self.directory.name) / 'VERSION.json'
        self.path_patch = patch.object(coverage_tables, 'VERSION_PATH', self.version)
        self.path_patch.start()
        self.addCleanup(self.path_patch.stop)

    def write_release(self, version):
        self.version.write_text(json.dumps({
            'workbench_version': version,
            'sync_protocol_version': 'TEST-SYNC',
            'card_protocol_version': 'TEST-CARD',
        }), encoding='utf-8')

    def test_coverage_uses_current_manifest_for_all_platforms(self):
        self.write_release('V9.41')
        text = coverage_tables.render({'A': {'2026-09-20'}}, [(2026, 9)])
        self.assertIn('| GitHub | V9.41 |', text)
        self.assertIn('| OpenClaw | V9.41 / TEST-SYNC |', text)
        self.assertIn('| 飞书 | V9.41 / TEST-CARD |', text)
        self.write_release('V9.42')
        self.assertIn('| GitHub | V9.42 |', coverage_tables.render({}, [(2026, 9)]))

    def test_current_version_block_uses_manifest_even_if_document_is_older(self):
        self.write_release('V9.43')
        older = ('## 三端当前版本与同步状态（自动维护）\n\n'
                 '| GitHub | **V1.17**（由规则源自动读取） | commit |\n'
                 '| OpenClaw | **V1.17**（运行时） | protocols |\n'
                 '| 飞书 | **V1.17**（卡片） | card |\n'
                 '历史消息不自动改写。\n\n## 历史版本\nV1.17\n')
        with patch.object(Path, 'read_text', side_effect=[
            self.version.read_text(encoding='utf-8'), older,
        ]):
            text = deterministic_upload._version_status_block()
        self.assertEqual(text.count('**V9.43**'), 3)
        self.assertNotIn('**V1.17**', text)
        self.assertIn('历史消息不自动改写。', text)
        self.assertNotIn('## 历史版本', text)

    def test_version_block_fallback_uses_manifest(self):
        self.write_release('V9.44')
        with patch.object(Path, 'read_text', side_effect=[
            self.version.read_text(encoding='utf-8'), OSError('no rules document'),
        ]):
            text = deterministic_upload._version_status_block()
        self.assertEqual(text.count('**V9.44**'), 3)
        self.assertIn('TEST-SYNC / TEST-CARD', text)

    def test_missing_manifest_is_visible_as_unverified_not_an_old_release(self):
        text = coverage_tables.render({}, [(2026, 9)])
        self.assertIn('| GitHub | 未核实 |', text)
        self.assertNotIn('V1.17', text)


if __name__ == '__main__':
    unittest.main()
