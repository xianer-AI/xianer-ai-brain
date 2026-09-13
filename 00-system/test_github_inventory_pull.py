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

    def incoming_update(self):
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
        main.write_text(text + "\n同步检查：仅 fixture 测试记录。\n", encoding="utf-8")
        project = self.writer / pull.PROJECT
        project.write_text(project.read_text().replace(f"共 {total:,} 双", f"共 {total + 17:,} 双"))
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

    def test_no_change(self):
        self.assertEqual(self.receive()["status"], "unchanged")
        self.assert_head_preserved()
        self.assertEqual(git(self.computer, "status", "--porcelain"), "")

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
