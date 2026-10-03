import importlib.util
import tempfile
import unittest
from pathlib import Path


SPEC = importlib.util.spec_from_file_location(
    "router", Path(__file__).with_name("router.py")
)
router = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(router)


class RouterBehaviorTests(unittest.TestCase):
    def test_d_identity_notice_is_work_routed(self):
        self.assertIsNotNone(router.IDENTITY_NOTICE.fullmatch('D=张小翠'))

    def test_plain_greeting_in_active_group_can_reach_xiaowen(self):
        with tempfile.TemporaryDirectory() as d:
            result = router.decide(
                str(Path(d) / "routing.sqlite"),
                {"id": "om-greeting", "sender": "ou-user", "text": "你好"},
                now=1000,
            )
        self.assertEqual(result["owner"], "xiaowen")

    def test_continue_is_work_followup_without_mention(self):
        with tempfile.TemporaryDirectory() as d:
            result = router.decide(
                str(Path(d) / "routing.sqlite"),
                {"id": "om-continue", "sender": "ou-user", "text": "继续"},
                now=1000,
            )
        self.assertEqual(result, {"owner": "xiaowen", "reason": "work_review"})

if __name__ == "__main__":
    unittest.main()
