"""Run with: python3 -m unittest discover -s 00-system -p test_mobile_inventory.py"""

import json
from datetime import date, timedelta
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import mobile_inventory as inventory


class MobileInventoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        fixture_root = Path(__file__).resolve().parents[1]
        for relative in inventory.BUSINESS_FILES:
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(fixture_root / relative, target)
        self.state = {
            "version": 1, "thread_id": inventory.THREAD_ID,
            "baseline": {"message_id": "prior-message", "started_at": 1},
            "events": [], "observed_message_ids": ["prior-message"],
            "receipt_message_ids": ["prior-receipt"], "custom": {"preserved": True},
        }
        (self.root / inventory.STATE).parent.mkdir(parents=True, exist_ok=True)
        (self.root / inventory.STATE).write_text(inventory.json_text(self.state), encoding="utf-8")
        self.initial = inventory.snapshot(self.root)

    def command(self, message="message-1", operation="add", quantity=500, **changes):
        result = {
            "thread_id": inventory.THREAD_ID, "message_id": message,
            "source_text": "黑色冰冰袜增加500双", "color": "黑色",
            "operation": operation, "quantity": quantity, "date": self.initial["updated_at"],
        }
        result.update(changes)
        return result

    def state_now(self):
        return json.loads((self.root / inventory.STATE).read_text(encoding="utf-8"))

    def all_contents(self):
        return {path: (self.root / path).read_text(encoding="utf-8") for path in (*inventory.BUSINESS_FILES, inventory.STATE)}

    def test_add_subtract_set_and_preserve_state(self):
        black = self.initial["colors"]["黑色"]
        total = self.initial["total"]
        for message, operation, quantity, expected in (
            ("first", "add", 500, black + 500),
            ("second", "subtract", 200, black + 300),
            ("third", "set", 123, 123),
        ):
            result = inventory.apply(self.root, self.command(message, operation, quantity))
            current = inventory.snapshot(self.root)
            self.assertFalse(result["duplicate"])
            self.assertEqual(current["colors"]["黑色"], expected)
            self.assertEqual(current["total"], total - black + expected)
            self.assertIn(f"共 {current['total']:,} 双", (self.root / inventory.PROJECT).read_text())
            self.assertIn(f"总库存 {current['total']:,} 双", (self.root / inventory.INDEX).read_text())
        self.assertEqual(len(self.state_now()["events"]), 3)
        self.assertEqual(self.state_now()["baseline"], self.state["baseline"])
        self.assertEqual(self.state_now()["custom"], self.state["custom"])

    def test_same_message_is_not_applied_twice(self):
        command = self.command()
        inventory.apply(self.root, command)
        before = self.all_contents()
        self.assertTrue(inventory.apply(self.root, command)["duplicate"])
        self.assertEqual(self.all_contents(), before)
        with self.assertRaises(inventory.InventoryError):
            inventory.apply(self.root, self.command(quantity=501))

    def test_repeated_text_with_different_ids_is_two_real_commands(self):
        inventory.apply(self.root, self.command("first"))
        inventory.apply(self.root, self.command("second"))
        self.assertEqual(inventory.snapshot(self.root)["total"], self.initial["total"] + 1000)

    def test_invalid_commands_do_not_modify_files(self):
        before = self.all_contents()
        invalid = [
            self.command(quantity=-1), self.command(quantity=True), self.command(quantity=1.5),
            self.command(operation="subtract", quantity=self.initial["colors"]["黑色"] + 1),
            self.command(thread_id="another-chat"), self.command(color="新颜色"),
            self.command(message="prior-message"), self.command(message="prior-receipt"),
            self.command(date="2026-02-30"), self.command(source_text="[库存同步回执] 黑色增加500双"),
            self.command(source_text="【电脑库存核验回执｜测试】黑色增加500双"),
        ]
        for command in invalid:
            with self.subTest(command=command), self.assertRaises(inventory.InventoryError):
                inventory.apply(self.root, command)
            self.assertEqual(self.all_contents(), before)
            self.assertFalse((self.root / inventory.JOURNAL).exists())

    def test_wrong_summary_refuses_write(self):
        target = self.root / inventory.MAIN
        text = target.read_text(encoding="utf-8")
        text = text.replace(
            f"| 已录入成品库存 | {self.initial['total']:,} 双 |",
            "| 已录入成品库存 | 1 双 |",
        )
        target.write_text(text, encoding="utf-8")
        before = self.all_contents()
        with self.assertRaises(inventory.InventoryError):
            inventory.apply(self.root, self.command())
        self.assertEqual(self.all_contents(), before)

    def test_github_primary_mode_blocks_apply_before_recovery(self):
        state = self.state_now()
        state["mode"] = "github_primary_computer_receive_only"
        (self.root / inventory.STATE).write_text(inventory.json_text(state), encoding="utf-8")
        journal = self.root / inventory.JOURNAL
        for pending in (False, True):
            with self.subTest(pending_journal=pending):
                if pending:
                    journal.write_text("{\"old_transaction\": true}\n", encoding="utf-8")
                before = self.all_contents()
                with patch.object(inventory, "recover_locked") as recover:
                    with self.assertRaisesRegex(inventory.InventoryError, "GitHub"):
                        inventory.apply(self.root, self.command())
                    recover.assert_not_called()
                self.assertEqual(self.all_contents(), before)
                self.assertEqual(journal.exists(), pending)
                if pending:
                    self.assertEqual(journal.read_text(), "{\"old_transaction\": true}\n")

    def test_github_primary_mode_blocks_direct_pending_recovery(self):
        state_before = (self.root / inventory.STATE).read_text(encoding="utf-8")
        old_transaction = inventory.build_transaction(
            self.root, self.command(), json.loads(state_before), state_before,
        )
        journal = self.root / inventory.JOURNAL
        journal.write_text(inventory.json_text(old_transaction), encoding="utf-8")
        state = self.state_now()
        state["mode"] = "github_primary_computer_receive_only"
        (self.root / inventory.STATE).write_text(inventory.json_text(state), encoding="utf-8")
        before = self.all_contents()
        journal_before = journal.read_text(encoding="utf-8")
        with inventory.locked(self.root):
            with self.assertRaisesRegex(inventory.InventoryError, "GitHub"):
                inventory.recover_locked(self.root)
        self.assertEqual(self.all_contents(), before)
        self.assertEqual(journal.read_text(encoding="utf-8"), journal_before)
        journal.unlink()
        with inventory.locked(self.root):
            self.assertIsNone(inventory.recover_locked(self.root))
        self.assertEqual(self.all_contents(), before)

    def test_interruption_after_each_transaction_file_recovers_once(self):
        original_write = inventory.atomic_write
        for stop_after in range(1, 7):
            with self.subTest(stop_after=stop_after):
                before = self.all_contents()
                tomorrow = date.fromisoformat(self.initial["updated_at"]) + timedelta(days=1)
                command = self.command(f"crash-{stop_after}", date=tomorrow.isoformat())
                calls = 0

                def crash_after_write(path, text):
                    nonlocal calls
                    original_write(path, text)
                    calls += 1
                    if calls == stop_after:
                        raise OSError("simulated interruption")

                with patch.object(inventory, "atomic_write", side_effect=crash_after_write):
                    with self.assertRaises(OSError):
                        inventory.apply(self.root, command)
                self.assertTrue((self.root / inventory.JOURNAL).exists())
                result = inventory.apply(self.root, command)
                self.assertTrue(result["duplicate"])
                self.assertEqual(inventory.snapshot(self.root)["total"], self.initial["total"] + 500)
                self.assertEqual(len(self.state_now()["events"]), 1)
                self.assertFalse((self.root / inventory.JOURNAL).exists())
                for path, content in before.items():
                    (self.root / path).write_text(content, encoding="utf-8")

    def test_recovery_preserves_concurrent_edit(self):
        original_write = inventory.atomic_write

        def stop_after_main(path, text):
            original_write(path, text)
            if path == self.root / inventory.MAIN:
                raise OSError("simulated interruption")

        with patch.object(inventory, "atomic_write", side_effect=stop_after_main):
            with self.assertRaises(OSError):
                inventory.apply(self.root, self.command())
        target = self.root / inventory.PROJECT
        target.write_text(target.read_text() + "\n用户并行添加的笔记\n", encoding="utf-8")
        before = self.all_contents()
        with self.assertRaises(inventory.InventoryError):
            inventory.apply(self.root, self.command())
        self.assertEqual(self.all_contents(), before)
        self.assertTrue((self.root / inventory.JOURNAL).exists())


if __name__ == "__main__":
    unittest.main()
