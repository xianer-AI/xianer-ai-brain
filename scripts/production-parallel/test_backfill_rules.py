import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import backfill_flow


class BackfillRuleTests(unittest.TestCase):
    def test_card_has_only_three_choices(self):
        card = backfill_flow.verification_card("B", "2026-09-20")
        content = card["elements"][0]["text"]["content"]
        self.assertIn("1. 当天未上班", content)
        self.assertIn("2. 已经报过", content)
        self.assertIn("3. 需要补报", content)
        self.assertNotIn("4. 不确定", content)
        self.assertEqual(len(card["elements"][1]["actions"]), 3)

    def test_verification_card_lists_queue_but_keeps_one_current_date(self):
        card = backfill_flow.verification_card(
            "B", "2026-09-20", ["2026-09-20", "2026-09-21"])
        content = card["elements"][0]["text"]["content"]
        self.assertIn("待核实日期共 **2 天**：2026-09-20、2026-09-21", content)
        self.assertIn("本次只处理当前日期；当前日期处理完成后，再发送下一张。", content)
        self.assertEqual(content.count("待核实生产日期：**2026-09-20**"), 1)

    def test_legacy_uncertain_choice_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "flow.sqlite"
            with sqlite3.connect(db) as conn:
                backfill_flow.init(db)
                conn.execute(
                    "INSERT INTO requests(request_key,worker,production_date,group_id,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                    ("B:2026-09-20:V1.17", "B", "2026-09-20", backfill_flow.GROUP,
                     "verification_sent", 1, 1),
                )
            self.assertIsNone(backfill_flow.handle_choice("missing", "sender", "4", db=db))

    def test_all_three_choices_are_idempotent_and_routed(self):
        for number, expected in (("1", "not_worked_review_sent"),
                                 ("2", "already_reported_review_sent"),
                                 ("3", "template_sent")):
            with self.subTest(number=number), tempfile.TemporaryDirectory() as tmp:
                db = Path(tmp) / "flow.sqlite"
                backfill_flow.init(db)
                with sqlite3.connect(db) as conn:
                    conn.execute(
                        "INSERT INTO requests(request_key,worker,production_date,group_id,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                        ("B:2026-09-20:V1.17", "B", "2026-09-20", backfill_flow.GROUP,
                         "verification_sent", 1, 1),
                    )
                if number == "3":
                    with patch.object(backfill_flow, "_send", return_value="om_template"):
                        result = backfill_flow.handle_choice("card-action-x", "sender", "生产补报 3 B 2026-09-20", db=db)
                else:
                    with patch.object(backfill_flow, "_create_status_review", return_value={"message_id": "om_review"}):
                        result = backfill_flow.handle_choice("card-action-x", "sender", f"生产补报 {number} B 2026-09-20", db=db)
                self.assertTrue(result["handled"])
                with sqlite3.connect(db) as conn:
                    self.assertEqual(conn.execute("SELECT status FROM requests").fetchone()[0], expected)

    def test_visible_button_fallback_sends_one_complete_template(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "flow.sqlite"
            backfill_flow.init(db)
            with sqlite3.connect(db) as conn:
                conn.execute(
                    "INSERT INTO requests(request_key,worker,production_date,group_id,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                    ("B:2026-09-20:V1.17", "B", "2026-09-20", backfill_flow.GROUP,
                     "verification_sent", 1, 1),
                )
            with patch.object(backfill_flow, "_send", return_value="om_template") as send:
                # Some Feishu clients send only the visible label and a
                # synthetic callback id.  It must still route deterministically.
                result = backfill_flow.handle_choice("synthetic-button-event", "sender", "3 需要补报", db=db)
            self.assertTrue(result["handled"])
            self.assertEqual(result["message_id"], "om_template")
            self.assertEqual(send.call_args.args[0]["header"]["title"]["content"], "补报生产数据")
            with sqlite3.connect(db) as conn:
                self.assertEqual(conn.execute("SELECT status FROM requests").fetchone()[0], "template_sent")

    def test_template_card_is_the_canonical_six_field_card(self):
        card = backfill_flow.template_card("B", "2026-09-20")
        self.assertEqual(card["header"]["title"]["content"], "补报生产数据")
        content = card["elements"][0]["content"]
        self.assertIn("六项必须全部保留", content)
        self.assertIn("B=梅芳", content)
        self.assertIn("棉堆堆袜：", content)
        self.assertIn("男船袜：", content)
        self.assertIn("回复“准确”后才上传", content)

    def test_complete_template_creates_backfill_review_card(self):
        with tempfile.TemporaryDirectory() as tmp:
            request_db = Path(tmp) / "backfill.sqlite"
            queue_db = Path(tmp) / "inbox.sqlite"
            old_db, old_primary = backfill_flow.DB, backfill_flow.PRIMARY_DB
            backfill_flow.DB, backfill_flow.PRIMARY_DB = request_db, queue_db
            try:
                backfill_flow.init(request_db)
                with sqlite3.connect(request_db) as conn:
                    conn.execute(
                        "INSERT INTO requests(request_key,worker,production_date,group_id,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                        ("B:2026-09-20:V1.17", "B", "2026-09-20", backfill_flow.GROUP,
                         "template_sent", 1, 1),
                    )
                import queue_store, review_cards, worker_identity
                queue_store.init(str(queue_db)); review_cards.init(str(queue_db)); worker_identity.init(str(queue_db))
                employee = "ou_b"
                with queue_store.conn(str(queue_db)) as conn:
                    conn.execute("INSERT INTO worker_identity(worker,name,platform,owner,proof,created) VALUES(?,?,?,?,?,?)",
                                 ("B", "梅芳", employee, "test", "test", 1))
                text = "\n".join([
                    "补报", "B=梅芳", "生产日期：2026-09-20", "工序：下机",
                    "棉堆堆袜：2400", "冰冰袜：1800", "小腿袜：300",
                    "过膝袜：0", "女船袜：0", "男船袜：0",
                ])
                with patch.object(review_cards, "deliver_card", return_value={"status": "sent", "messageId": "om_review"}):
                    result = backfill_flow.handle_template("om_template", employee, text, db=request_db)
                self.assertTrue(result["handled"])
                self.assertEqual(result["message_id"], "om_review")
                row = queue_store.get(str(queue_db), "om_template")
                self.assertEqual(row["sender"], employee)
                self.assertTrue(row["result"]["extracted"]["backfill"])
                self.assertEqual(row["result"]["extracted"]["items"][-1]["quantity"], 0)
            finally:
                backfill_flow.DB, backfill_flow.PRIMARY_DB = old_db, old_primary

    def test_incomplete_template_reports_missing_fields_without_acknowledging_upload(self):
        text = "\n".join([
            "补报", "B=梅芳", "生产日期：2026-09-20", "工序：下机",
            "棉堆堆袜：2400", "冰冰袜：1800", "小腿袜：300",
            "过膝袜：", "女船袜：", "男船袜：",
        ])
        result = backfill_flow.handle_template("om_template", "ou_b", text)
        self.assertTrue(result["handled"])
        self.assertIn("过膝袜、女船袜、男船袜", result["text"])

    def test_status_choices_use_shared_primary_queue_and_upload_safe_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            request_db = Path(tmp) / "backfill.sqlite"
            queue_db = Path(tmp) / "inbox.sqlite"
            old_db, old_primary = backfill_flow.DB, backfill_flow.PRIMARY_DB
            backfill_flow.DB, backfill_flow.PRIMARY_DB = request_db, queue_db
            backfill_flow.init(request_db)
            with sqlite3.connect(request_db) as conn:
                conn.execute(
                    "INSERT INTO requests(request_key,worker,production_date,group_id,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                    ("B:2026-09-20:V1.17", "B", "2026-09-20", backfill_flow.GROUP,
                     "verification_sent", 1, 1),
                )
            import queue_store
            import review_cards
            import worker_identity
            queue_store.init(str(queue_db))
            review_cards.init(str(queue_db))
            worker_identity.init(str(queue_db))
            event = {
                "messageId": "om_status_source",
                "senderId": "ou_b",
                "groupId": backfill_flow.GROUP,
                "content": "当天未上班：2026-09-20",
            }
            result = {
                "extracted": {
                    "kind": "report", "worker": "B", "production_date": "2026-09-20",
                    "items": [{"product": product, "process": "下机", "quantity": 0}
                              for product in backfill_flow.PRODUCTS],
                    "backfill": False,
                }
            }
            import hashlib, json
            raw = json.dumps(event, ensure_ascii=False, sort_keys=True)
            digest = hashlib.sha256(json.dumps({k: event[k] for k in ('messageId','senderId','groupId','content')}, sort_keys=True).encode()).hexdigest()
            with queue_store.conn(str(queue_db)) as conn:
                conn.execute("INSERT INTO inbox(id,sender,grp,event,digest,status,result,created) VALUES(?,?,?,?,?,?,?,?)",
                             (event["messageId"], event["senderId"], backfill_flow.GROUP, raw, digest,
                              "ready", json.dumps(result, ensure_ascii=False), 1))
                conn.execute("INSERT INTO worker_identity(worker,name,platform,owner,proof,created) VALUES(?,?,?,?,?,?)",
                             ("B", "梅芳", event["senderId"], "test", "test", 1))
            with patch.object(review_cards, "deliver_card", return_value={"status": "sent", "messageId": "om_review"}):
                issued = backfill_flow._create_status_review("B", "2026-09-20", event["senderId"], "not_worked", db=request_db)
            self.assertEqual(issued["message_id"], "om_review")
            self.assertTrue(issued["source"].startswith("om_backfill_status_"))
            self.assertIsNotNone(queue_store.get(str(queue_db), issued["source"]))
            backfill_flow.DB, backfill_flow.PRIMARY_DB = old_db, old_primary

    def test_owner_status_choice_binds_review_to_target_employee(self):
        with tempfile.TemporaryDirectory() as tmp:
            request_db = Path(tmp) / "backfill.sqlite"
            queue_db = Path(tmp) / "inbox.sqlite"
            old_db, old_primary = backfill_flow.DB, backfill_flow.PRIMARY_DB
            backfill_flow.DB, backfill_flow.PRIMARY_DB = request_db, queue_db
            try:
                backfill_flow.init(request_db)
                with sqlite3.connect(request_db) as conn:
                    conn.execute(
                        "INSERT INTO requests(request_key,worker,production_date,group_id,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                        ("B:2026-09-20:V1.17", "B", "2026-09-20", backfill_flow.GROUP,
                         "verification_sent", 1, 1),
                    )
                import queue_store, review_cards, worker_identity
                queue_store.init(str(queue_db))
                review_cards.init(str(queue_db))
                worker_identity.init(str(queue_db))
                owner = review_cards.OWNER
                employee = "ou_b"
                with queue_store.conn(str(queue_db)) as conn:
                    conn.execute("INSERT INTO worker_identity(worker,name,platform,owner,proof,created) VALUES(?,?,?,?,?,?)",
                                 ("B", "梅芳", employee, "test", "test", 1))
                with patch.object(review_cards, "deliver_card", return_value={"status": "sent", "messageId": "om_review"}):
                    issued = backfill_flow._create_status_review("B", "2026-09-20", owner, "already_reported", db=request_db)
                self.assertEqual(issued["message_id"], "om_review")
                row = queue_store.get(str(queue_db), issued["source"])
                self.assertEqual(row["sender"], employee)
            finally:
                backfill_flow.DB, backfill_flow.PRIMARY_DB = old_db, old_primary

    def test_advance_sends_only_next_queued_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "flow.sqlite"
            backfill_flow.init(db)
            with sqlite3.connect(db) as conn:
                for day, status in (("2026-09-20", "verification_sent"),
                                     ("2026-09-21", "queued_after_previous"),
                                     ("2026-09-22", "queued_after_previous")):
                    conn.execute(
                        "INSERT INTO requests(request_key,worker,production_date,group_id,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                        (f"B:{day}:V1.17", "B", day, backfill_flow.GROUP, status, 1, 1),
                    )
            with patch.object(backfill_flow, "_send", return_value="om_next"):
                # The production auto queue is intentionally independent of
                # this isolated temp database; keep the unit test deterministic.
                with patch("missing_alerts.has_pending_alert", return_value=False):
                    result = backfill_flow.advance_after_upload("B", "2026-09-20", db=db)
            self.assertEqual(result["status"], "advanced")
            self.assertEqual(result["next"], "2026-09-21")
            with sqlite3.connect(db) as conn:
                statuses = dict(conn.execute("SELECT production_date,status FROM requests"))
            self.assertEqual(statuses["2026-09-20"], "uploaded")
            self.assertEqual(statuses["2026-09-21"], "verification_sent")
            self.assertEqual(statuses["2026-09-22"], "queued_after_previous")


if __name__ == "__main__":
    unittest.main()
