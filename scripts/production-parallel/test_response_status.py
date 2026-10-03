import unittest

from response_status import render_batch_status


class ResponseStatusTests(unittest.TestCase):
    def test_completed_status_does_not_mention_confirmation(self):
        text = render_batch_status({
            'state': 'completed', 'commit': 'abc123',
            'reason': 'GitHub 已提交并完成远程回读',
        })
        self.assertIn('这批已完成', text)
        self.assertNotIn('待确认', text)

    def test_pending_status_requests_one_confirmation(self):
        text = render_batch_status({
            'state': 'awaiting_confirmation',
            'reason': '等待员工确认',
        })
        self.assertIn('请核对后回复“准确”', text)

    def test_unknown_delivery_requests_reconciliation_without_resend(self):
        text = render_batch_status({
            'state': 'needs_reconciliation',
            'reason': '飞书核对卡送达结果不明确，需要先核实',
        })
        self.assertIn('先核实', text)
        self.assertNotIn('请重复回复', text)

    def test_upload_failure_keeps_confirmation_valid(self):
        text = render_batch_status({
            'state': 'upload_failed',
            'reason': '已确认，上传因台账月份结构失败',
        })
        self.assertIn('已确认，上传因台账月份结构失败', text)
        self.assertNotIn('重新', text)
        self.assertNotIn('重报', text)


if __name__ == '__main__':
    unittest.main()
