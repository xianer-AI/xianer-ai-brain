import json
import sqlite3
import sys
import tempfile
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import missing_alerts


class MissingAlertRecallTests(unittest.TestCase):
    def test_recalled_automatic_card_is_resent_with_new_uuid(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "inbox.sqlite")
            alert = {
                "alert_key": "B:2026-09-20",
                "worker": "B",
                "production_date": "2026-09-20",
                "name": "梅芳",
                "status": "待核实",
                "check_at": "",
                "message": "补报：2026-09-20",
            }
            calls = []

            def request(_actor, method, path, data=None):
                calls.append((method, path, data))
                if method == "GET":
                    return {"data": {"items": [{"deleted": True}]}}
                return {"data": {"message_id": "om_resend"}}

            def build(*_args, **_kwargs):
                return [alert]

            with patch.object(missing_alerts, "build_alerts", build), \
                 patch.object(missing_alerts, "_ledger_snapshot", return_value=""), \
                 patch.dict(sys.modules, {"transport": types.SimpleNamespace(request=request)}), \
                 patch("transport.request", side_effect=request), \
                 patch("backfill_flow.register_verification"):
                first = missing_alerts.deliver_scheduled_alerts(
                    db=db,
                    now=datetime(2026, 10, 1, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
                    ledger_markdown="provided",
                )
                with sqlite3.connect(db) as conn:
                    old_uuid = conn.execute(
                        "SELECT delivery_uuid FROM scheduled_missing_alerts WHERE alert_key=?",
                        (alert["alert_key"],),
                    ).fetchone()[0]
                    conn.execute(
                        "UPDATE scheduled_missing_alerts SET state='sent',message_id='om_old',sent_at=1 WHERE alert_key=?",
                        (alert["alert_key"],),
                    )
                second = missing_alerts.deliver_scheduled_alerts(
                    db=db,
                    now=datetime(2026, 10, 1, 12, 1, tzinfo=ZoneInfo("Asia/Shanghai")),
                    ledger_markdown="provided",
                )
                with sqlite3.connect(db) as conn:
                    state, message_id, new_uuid = conn.execute(
                        "SELECT state,message_id,delivery_uuid FROM scheduled_missing_alerts WHERE alert_key=?",
                        (alert["alert_key"],),
                    ).fetchone()

            self.assertEqual(first, [alert["alert_key"]])
            self.assertEqual(second, [alert["alert_key"]])
            self.assertNotEqual(old_uuid, new_uuid)
            self.assertEqual((state, message_id), ("sent", "om_resend"))
            post_calls = [call for call in calls if call[0] == "POST"]
            self.assertEqual(len(post_calls), 2)
            self.assertNotEqual(post_calls[0][2]["uuid"], post_calls[1][2]["uuid"])

            # Automatic reminders must start with the same 1/2/3 verification
            # card as the manual flow.  The six-field fill-in template is
            # only the second stage after choice 3.
            for call in post_calls:
                payload = json.loads(call[2]["content"])
                self.assertEqual(
                    payload["header"]["title"]["content"], "补发生产日期待核实"
                )
                action = next(
                    element for element in payload["elements"]
                    if element.get("tag") == "action"
                )
                labels = [item["text"]["content"] for item in action["actions"]]
                self.assertEqual(labels, ["1 当天未上班", "2 已经报过", "3 需要补报"])

    def test_multi_day_queue_shows_all_dates_then_advances_one_by_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "inbox.sqlite")
            alerts = [
                {"alert_key": "B:2026-10-01", "worker": "B",
                 "production_date": "2026-10-01", "name": "梅芳",
                 "status": "待核实", "check_at": "",
                 "message": "补报：2026-10-01"},
                {"alert_key": "B:2026-10-02", "worker": "B",
                 "production_date": "2026-10-02", "name": "梅芳",
                 "status": "待核实", "check_at": "",
                 "message": "补报：2026-10-02"},
            ]
            posts = []

            def request(_actor, method, path, data=None):
                if method == "POST":
                    posts.append(data)
                    return {"data": {"message_id": f"om_card_{len(posts)}"}}
                return {"data": {"items": [{"deleted": False}]}}

            def build(*_args, **_kwargs):
                return alerts

            with patch.object(missing_alerts, "build_alerts", build), \
                 patch.object(missing_alerts, "_ledger_snapshot", return_value=""), \
                 patch.dict(sys.modules, {"transport": types.SimpleNamespace(request=request)}), \
                 patch("transport.request", side_effect=request), \
                 patch("backfill_flow.register_verification"):
                first = missing_alerts.deliver_scheduled_alerts(
                    db=db,
                    now=datetime(2026, 10, 3, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
                    ledger_markdown="provided",
                )
                self.assertEqual(first, ["B:2026-10-01"])
                first_payload = json.loads(posts[0]["content"])
                first_text = first_payload["elements"][0]["text"]["content"]
                self.assertIn("待核实日期共 **2 天**：2026-10-01、2026-10-02", first_text)
                self.assertIn("待核实生产日期：**2026-10-01**", first_text)

                self.assertTrue(
                    missing_alerts.mark_receipt_confirmed(
                        "B", "2026-10-01", "om_receipt", db=db
                    )
                )
                second = missing_alerts.deliver_scheduled_alerts(
                    db=db,
                    now=datetime(2026, 10, 3, 12, 1, tzinfo=ZoneInfo("Asia/Shanghai")),
                    ledger_markdown="provided",
                )
                self.assertEqual(second, ["B:2026-10-02"])
                second_payload = json.loads(posts[1]["content"])
                second_text = second_payload["elements"][0]["text"]["content"]
                self.assertIn("待核实日期共 **1 天**：2026-10-02", second_text)
                self.assertIn("待核实生产日期：**2026-10-02**", second_text)


if __name__ == "__main__":
    unittest.main()
