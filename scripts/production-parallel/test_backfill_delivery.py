import tempfile
import unittest
from pathlib import Path

import backfill_delivery


class BackfillDeliveryTests(unittest.TestCase):
    def test_card_tells_employee_to_copy_and_only_replace_numbers(self):
        payload = backfill_delivery.card("B", "2026-09-20")
        element = payload["elements"][0]
        content = element.get("content") or element.get("text", {}).get("content", "")
        self.assertIn("六个产品名称后的冒号后填写数字", content)
        self.assertIn("B=梅芳", content)
        self.assertIn("生产日期：2026-09-20", content)
        for product in backfill_delivery.PRODUCTS:
            self.assertIn(product + "：", content)
        self.assertIn("未上班：2026-09-20", content)
        self.assertIn("已报待查：2026-09-20", content)

    def test_delivery_is_idempotent_per_employee_and_date(self):
        with tempfile.TemporaryDirectory() as root:
            db = Path(root) / "alerts.sqlite"
            calls = []

            def send(payload, token):
                calls.append((payload, token))
                return "om_alert"

            first = backfill_delivery.deliver(db, "B", "2026-09-20", send=send)
            second = backfill_delivery.deliver(db, "B", "2026-09-20", send=send)
            self.assertEqual(first["status"], "sent")
            self.assertEqual(second["status"], "already_sent")
            self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
