import tempfile
import unittest
from pathlib import Path

import worker_identity


class WorkerIdentityDTests(unittest.TestCase):
    def test_parse_self_declaration_accepts_zhang_xiaocui_as_d(self):
        self.assertEqual(
            worker_identity.parse_self_declaration('D=张小翠'),
            ('D', '张小翠'),
        )

    def test_self_bind_persists_d_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            db = str(Path(directory) / 'inbox.sqlite')
            previous_legacy_state = worker_identity.LEGACY_STATE
            worker_identity.LEGACY_STATE = Path(directory) / 'legacy-state.json'
            try:
                result = worker_identity.self_bind(db, 'D', 'ou_zhang_xiaocui', 'om_bind_d')
            finally:
                worker_identity.LEGACY_STATE = previous_legacy_state
            self.assertEqual(result['bound'], 'D')
            self.assertEqual(result['name'], '张小翠')
            self.assertEqual(worker_identity.lookup(db, 'ou_zhang_xiaocui')['worker'], 'D')


if __name__ == '__main__':
    unittest.main()
