"""Temporary local repositories only; never fetch or change the real repository."""

from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

import github_inventory_pull as pull


REAL_FIXTURE = Path(__file__).resolve().parents[1]


def git(cwd, *args):
    result = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", *args],
        cwd=cwd, capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


class GithubInventoryPullTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.remote = self.base / "remote.git"
        self.writer = self.base / "phone"
        self.computer = self.base / "computer"
        git(self.base, "init", "--bare", "--initial-branch=main", str(self.remote))
        git(self.base, "clone", str(self.remote), str(self.writer))
        for key, value in (("user.name", "Fixture"), ("user.email", "fixture@example.invalid")):
            git(self.writer, "config", key, value)
        for relative in (pull.MAIN, pull.PROJECT):
            target = self.writer / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(REAL_FIXTURE / relative, target)
        self.commit_remote("initial fixture")
        git(self.base, "clone", str(self.remote), str(self.computer))
        for key, value in (("user.name", "Fixture"), ("user.email", "fixture@example.invalid")):
            git(self.computer, "config", key, value)
        self.initial = git(self.computer, "rev-parse", "HEAD")
        self.initial_inventory = pull.validate_inventory(
            (self.writer / pull.MAIN).read_text(), (self.writer / pull.PROJECT).read_text(),
        )

    def commit_remote(self, message):
        git(self.writer, "add", "--all")
        git(self.writer, "commit", "-m", message)
        git(self.writer, "push", "origin", "main")
        return git(self.writer, "rev-parse", "HEAD")

    def incoming_update(self, update_summary=True):
        main = self.writer / pull.MAIN
        total = self.initial_inventory["total"]
        black = self.initial_inventory["colors"]["黑色"]
        text = main.read_text().replace(
            f"| 已录入成品库存 | {total:,} 双 |", f"| 已录入成品库存 | {total + 17:,} 双 |",
        )
        text = re.sub(
            r"^(\| \d+ \| 黑色 \| [^|]+ \| 成品 \| )[\d,]+ 双",
            lambda match: f"{match.group(1)}{black + 17:,} 双", text, flags=re.M,
        )
        remaining = self.initial_inventory["remaining"]
        packaged = self.initial_inventory["packaged"]
        text = text.replace(f"| 剩余未包装半成品 | {remaining:,} 双 |", f"| 剩余未包装半成品 | {remaining - 17:,} 双 |")
        text = text.replace(f"| 累计已包装数量 | {packaged:,} 双 |", f"| 累计已包装数量 | {packaged + 17:,} 双 |")
        main.write_text(text + "\n同步检查：仅 fixture 测试记录。\n", encoding="utf-8")
        project = self.writer / pull.PROJECT
        if update_summary:
            project.write_text(project.read_text().replace(f"共 {total:,} 双", f"共 {total + 17:,} 双").replace(f"半成品 {remaining:,} 双", f"半成品 {remaining - 17:,} 双"))
        return self.commit_remote("fixture update")

    def receive(self):
        return pull.receive(self.computer, test_local_remote=True)

    def assert_head_preserved(self):
        self.assertEqual(git(self.computer, "rev-parse", "HEAD"), self.initial)

    def test_fast_forward_preserves_untracked_and_state_outside_worktree(self):
        settings = self.computer / ".obsidian" / "workspace.json"
        settings.parent.mkdir()
        settings.write_text('{"keep": true}\n')
        candidate = self.incoming_update()
        result = self.receive()
        self.assertEqual(result["status"], "fast_forwarded")
        self.assertEqual(result["commit"], candidate)
        self.assertEqual(result["inventory"]["total"], self.initial_inventory["total"] + 17)
        self.assertEqual(result["inventory"]["colors"]["黑色"], self.initial_inventory["colors"]["黑色"] + 17)
        self.assertEqual(settings.read_text(), '{"keep": true}\n')
        self.assertTrue((self.computer / ".git" / pull.STATE_NAME).exists())
        self.assertEqual(git(self.computer, "status", "--short"), "?? .obsidian/")

    def test_legacy_summary_label(self):
        main = (self.writer / pull.MAIN).read_text().replace("已录入成品库存", "已录入库存")
        self.assertEqual(pull.validate_inventory(main, (self.writer / pull.PROJECT).read_text()), self.initial_inventory)

    def test_new_color_is_accepted_when_ledger_is_consistent(self):
        main = (self.writer / pull.MAIN).read_text()
        main = main.replace("| 已录入颜色数 | 7 个 |", "| 已录入颜色数 | 8 个 |")
        main = main.replace("| 累计已包装数量 | 92,844 双 |", "| 累计已包装数量 | 101,344 双 |")
        main = main.replace("| 剩余未包装半成品 | 407,156 双 |", "| 剩余未包装半成品 | 398,656 双 |")
        main = main.replace("| 已录入成品库存 | 77,844 双 |", "| 已录入成品库存 | 86,344 双 |")
        main = main.replace(
            "| 7 | 天兰 | 冰冰袜（夏季堆堆袜） | 成品 | 9,950 双 | 2026-09-18 | 首次录入，按包装转入处理 |\n",
            "| 7 | 天兰 | 冰冰袜（夏季堆堆袜） | 成品 | 9,950 双 | 2026-09-18 | 首次录入，按包装转入处理 |\n"
            "| 8 | 奶黄 | 冰冰袜（夏季堆堆袜） | 成品 | 8,500 双 | 2026-09-18 | 首次录入，按包装转入处理 |\n",
        )
        inventory = pull.validate_inventory(main)
        self.assertEqual(inventory["total"], 86_344)
        self.assertEqual(inventory["colors"]["奶黄"], 8_500)

    def phone_format(self):
        main = (self.writer / pull.MAIN).read_text()
        main = main.replace("已录入成品库存", "当前成品库存")
        return re.sub(r"(?<=\d) 双(?= \|)", "", main)

    def test_phone_format_receives_once_with_identical_quantities(self):
        main = self.writer / pull.MAIN
        main.write_text(self.phone_format())
        candidate = self.commit_remote("phone quantity format")
        result = self.receive()
        self.assertEqual(result["commit"], candidate)
        self.assertEqual(result["inventory"], self.initial_inventory)
        self.assertEqual((self.computer / pull.MAIN).read_bytes(), main.read_bytes())
        self.assertEqual(self.receive()["status"], "unchanged")

    def test_bare_counts_require_current_pairs_declaration(self):
        main = self.phone_format()
        for declaration in ("", "数量单位：只", "数量单位：件"):
            invalid = main.replace("数量单位：双", declaration)
            with self.subTest(declaration=declaration), self.assertRaises(pull.PullError):
                pull.validate_inventory(invalid)
        without = main.replace("数量单位：双", "")
        with self.assertRaises(pull.PullError):
            pull.validate_inventory(without + "\n## 旧资料\n数量单位：双\n")

    def test_mixed_units_and_summary_aliases(self):
        main = self.phone_format()
        total = self.initial_inventory["total"]
        mixed = main.replace(f"| 当前成品库存 | {total:,} |", f"| 当前成品库存 | {total:,} 双 |")
        self.assertEqual(pull.validate_inventory(mixed), self.initial_inventory)
        for row in (f"| 已录入成品库存 | {total:,} 双 |", "| 当前成品库存 | -1 |", "| 剩余未包装半成品 | -1 双 |"):
            with self.subTest(row=row), self.assertRaises(pull.PullError):
                pull.validate_inventory(main + "\n" + row + "\n")

    def test_phone_format_rejects_wrong_counts_and_units(self):
        main = self.phone_format()
        total = self.initial_inventory["total"]
        for cell in ("-1", "1.5", "1,00", "", f"{total:,} 只", f"{total:,} 件"):
            invalid = main.replace(f"| 当前成品库存 | {total:,} |", f"| 当前成品库存 | {cell} |")
            with self.subTest(cell=cell), self.assertRaises(pull.PullError):
                pull.validate_inventory(invalid)

    def test_no_change(self):
        self.assertEqual(self.receive()["status"], "unchanged")
        self.assert_head_preserved()
        self.assertEqual(git(self.computer, "status", "--porcelain"), "")

    def test_stale_summary_receives_without_replaying_and_later_clears(self):
        candidate = self.incoming_update(update_summary=False)
        result = self.receive()
        self.assertEqual(result["commit"], candidate)
        self.assertEqual(result["status"], "fast_forwarded")
        self.assertTrue(result["warnings"])
        self.assertEqual((self.computer / pull.MAIN).read_bytes(), (self.writer / pull.MAIN).read_bytes())
        again = self.receive()
        self.assertEqual(again["status"], "unchanged")
        self.assertEqual(again["inventory"], result["inventory"])
        self.assertEqual(git(self.computer, "status", "--porcelain"), "")
        inventory = result["inventory"]
        (self.writer / pull.PROJECT).write_text(
            f"已录入 6 个颜色，共 {inventory['total']:,} 双成品，剩余未包装半成品 {inventory['remaining']:,} 双，账面合计 {inventory['combined']:,} 双\n")
        self.commit_remote("complete derived summary")
        self.assertEqual(self.receive()["warnings"], [])

    def test_missing_summary_does_not_block_ledger(self):
        self.incoming_update()
        (self.writer / pull.PROJECT).unlink()
        candidate = self.commit_remote("missing summary")
        result = self.receive()
        self.assertEqual(result["commit"], candidate)
        self.assertTrue(result["warnings"])

    def test_malformed_and_duplicate_summaries_are_warnings(self):
        self.assertTrue(pull.summary_warnings(self.initial_inventory, "摘要格式改变"))
        project = (self.writer / pull.PROJECT).read_text()
        self.assertTrue(pull.summary_warnings(self.initial_inventory, project + project))

    def test_summary_crlf_line_endings_receive(self):
        project = self.writer / pull.PROJECT
        project.write_bytes(project.read_bytes().replace(b"\n", b"\r\n"))
        candidate = self.commit_remote("summary CRLF")
        self.assertEqual(self.receive()["commit"], candidate)
        self.assertEqual((self.computer / pull.PROJECT).read_bytes(), project.read_bytes())

    def test_invalid_balance_and_required_fields_refused_before_checkout(self):
        main = self.writer / pull.MAIN
        original = main.read_text()
        remaining = self.initial_inventory["remaining"]
        packaged = self.initial_inventory["packaged"]
        row = f"| 剩余未包装半成品 | {remaining:,} 双 |"
        variants = [
            original.replace(row, f"| 剩余未包装半成品 | {remaining + 1:,} 双 |"),
            original.replace(row, "| 剩余未包装半成品 | -1 双 |"),
            original.replace(row, ""),
            original + "\n" + row + "\n",
            original.replace(f"| 累计已包装数量 | {packaged:,} 双 |", ""),
        ]
        for i, invalid in enumerate(variants):
            with self.subTest(i=i):
                main.write_text(invalid)
                self.commit_remote(f"invalid balance {i}")
                with self.assertRaises(pull.PullError):
                    self.receive()
                self.assert_head_preserved()

    def test_shipment_and_production_need_not_equal_opening_or_packaged(self):
        main = self.writer / pull.MAIN
        original = main.read_text()
        inventory = self.initial_inventory
        total, black = inventory["total"], inventory["colors"]["黑色"]
        # Shipment reduces finished stock and combined stock; packed stays fixed.
        shipped = original.replace(f"| 已录入成品库存 | {total:,} 双 |", f"| 已录入成品库存 | {total - 10:,} 双 |")
        shipped = re.sub(r"^(\| \d+ \| 黑色 \| [^|]+ \| 成品 \| )[\d,]+ 双", lambda m: f"{m.group(1)}{black - 10:,} 双", shipped, flags=re.M)
        shipped = shipped.replace(f"| 当前账面总库存（半成品＋成品） | {inventory['combined']:,} 双 |", f"| 当前账面总库存（半成品＋成品） | {inventory['combined'] - 10:,} 双 |")
        main.write_text(shipped)
        self.commit_remote("shipment")
        self.assertEqual(self.receive()["inventory"]["packaged"], inventory["packaged"])
        # New production raises unfinished and combined stock above the opening.
        produced = original.replace(f"| 剩余未包装半成品 | {inventory['remaining']:,} 双 |", f"| 剩余未包装半成品 | {inventory['remaining'] + 20:,} 双 |")
        produced = produced.replace(f"| 当前账面总库存（半成品＋成品） | {inventory['combined']:,} 双 |", f"| 当前账面总库存（半成品＋成品） | {inventory['combined'] + 20:,} 双 |")
        main.write_text(produced)
        self.commit_remote("production fixture")
        self.assertEqual(self.receive()["inventory"]["combined"], inventory["combined"] + 20)

    def test_tracked_dirty_and_index_dirty_refused(self):
        self.incoming_update()
        target = self.computer / pull.MAIN
        original = target.read_text()
        target.write_text(original + "\n电脑未提交笔记\n")
        with self.assertRaises(pull.PullError):
            self.receive()
        self.assertIn("电脑未提交笔记", target.read_text())
        self.assert_head_preserved()
        git(self.computer, "add", pull.MAIN)
        with self.assertRaises(pull.PullError):
            self.receive()
        self.assert_head_preserved()

    def test_local_divergence_refused(self):
        self.incoming_update()
        (self.computer / "local-note.txt").write_text("fixture local commit\n")
        git(self.computer, "add", "local-note.txt")
        git(self.computer, "commit", "-m", "local fixture divergence")
        local_head = git(self.computer, "rev-parse", "HEAD")
        with self.assertRaises(pull.PullError):
            self.receive()
        self.assertEqual(git(self.computer, "rev-parse", "HEAD"), local_head)

    def test_invalid_remote_inventory_refused_before_checkout(self):
        main = self.writer / pull.MAIN
        total = self.initial_inventory["total"]
        main.write_text(main.read_text().replace(f"| 已录入成品库存 | {total:,} 双 |", "| 已录入成品库存 | 1 双 |"))
        self.commit_remote("invalid fixture inventory")
        with self.assertRaises(pull.PullError):
            self.receive()
        self.assert_head_preserved()
        self.assertIn(f"| 已录入成品库存 | {total:,} 双 |", (self.computer / pull.MAIN).read_text())

    def test_untracked_collision_refused(self):
        local = self.computer / ".obsidian" / "workspace.json"
        local.parent.mkdir()
        local.write_text("local content\n")
        remote = self.writer / ".obsidian" / "workspace.json"
        remote.parent.mkdir()
        remote.write_text("incoming content\n")
        self.commit_remote("colliding fixture")
        with self.assertRaises(pull.PullError):
            self.receive()
        self.assertEqual(local.read_text(), "local content\n")
        self.assert_head_preserved()

    def test_ignored_untracked_collision_also_refused(self):
        (self.writer / ".gitignore").write_text("private-note.txt\n")
        self.commit_remote("fixture ignore rule")
        self.receive()
        before = git(self.computer, "rev-parse", "HEAD")
        (self.computer / "private-note.txt").write_text("keep local ignored file\n")
        (self.writer / "private-note.txt").write_text("remote tracked file\n")
        git(self.writer, "add", "--force", "private-note.txt")
        self.commit_remote("fixture collision with ignored file")
        with self.assertRaises(pull.PullError):
            self.receive()
        self.assertEqual(git(self.computer, "rev-parse", "HEAD"), before)
        self.assertEqual((self.computer / "private-note.txt").read_text(), "keep local ignored file\n")

    def test_pending_inventory_journal_and_wrong_branch_refused(self):
        journal = self.computer / pull.PENDING_JOURNAL
        journal.parent.mkdir(parents=True)
        journal.write_text("{}\n")
        with self.assertRaises(pull.PullError):
            self.receive()
        journal.unlink()
        git(self.computer, "switch", "-c", "fixture-other")
        with self.assertRaises(pull.PullError):
            self.receive()
        self.assert_head_preserved()

    def test_production_mode_rejects_local_remote(self):
        with self.assertRaises(pull.PullError):
            pull.receive(self.computer)
        self.assert_head_preserved()


if __name__ == "__main__":
    unittest.main()
