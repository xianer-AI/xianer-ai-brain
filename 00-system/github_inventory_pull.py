#!/usr/bin/env python3
"""Receive validated GitHub inventory commits by fast-forward only.

Production remote: github.com/xianer-AI/xianer-ai-brain, branch main.
No inventory arithmetic, commits, pushes, stashes, resets, or conflict resolution.
"""

import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile


MAIN = "袜子生产制造袜子厂/库存记录/2026下半年冰冰袜库存包装统计.md"
PROJECT = "袜子生产制造袜子厂/README.md"
INVENTORY_README = "袜子生产制造袜子厂/库存记录/README.md"
INDEX = "03-index/index.md"
ALLOWED_REMOTES = {
    "https://github.com/xianer-AI/xianer-ai-brain", "https://github.com/xianer-AI/xianer-ai-brain.git",
    "git@github.com:xianer-AI/xianer-ai-brain", "git@github.com:xianer-AI/xianer-ai-brain.git",
    "ssh://git@github.com/xianer-AI/xianer-ai-brain", "ssh://git@github.com/xianer-AI/xianer-ai-brain.git",
}
PENDING_JOURNAL = "00-system/.mobile-inventory-journal.json"
LOCK_NAME = "mobile-github-pull.lock"
STATE_NAME = "mobile-github-pull-state.json"


class PullError(RuntimeError):
    pass


def git(root, *arguments, test_local=False, accepted=(0,)):
    command = [
        "git", "-c", "protocol.ext.allow=never",
        "-c", f"protocol.file.allow={'always' if test_local else 'never'}",
        "-c", "submodule.recurse=false", *arguments,
    ]
    environment = dict(os.environ)
    environment["GIT_TERMINAL_PROMPT"] = "0"
    environment["GIT_NO_REPLACE_OBJECTS"] = "1"
    try:
        result = subprocess.run(
            command, cwd=root, env=environment, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=55,
        )
    except subprocess.TimeoutExpired as exc:
        raise PullError("Git 操作超时；保留电脑内容，稍后重试") from exc
    if result.returncode not in accepted:
        detail = result.stderr.decode("utf-8", errors="replace").strip()[:1500]
        raise PullError(f"Git {arguments[0]} 失败：{detail}")
    return result


def output(root, *arguments, **kwargs):
    return git(root, *arguments, **kwargs).stdout.decode("utf-8").strip()


def paths(root, *arguments):
    return [os.fsdecode(value) for value in git(root, *arguments).stdout.split(b"\0") if value]


def validate_remote(root, test_local):
    urls = output(root, "remote", "get-url", "--all", "origin").splitlines()
    if len(urls) != 1:
        raise PullError("origin 必须恰好只有一个远程地址")
    if test_local:
        remote = Path(urls[0])
        if not remote.is_absolute() or not remote.is_dir():
            raise PullError("测试开关只允许绝对路径形式的本地 bare 仓库")
        if output(root, "--git-dir", str(remote), "rev-parse", "--is-bare-repository", test_local=True) != "true":
            raise PullError("测试远端必须是本地 bare 仓库")
    elif urls[0] not in ALLOWED_REMOTES:
        raise PullError("生产模式仅允许 GitHub 仓库 xianer-AI/xianer-ai-brain，拒绝其他 origin")


def git_path(root, name):
    value = Path(output(root, "rev-parse", "--git-path", name))
    return value if value.is_absolute() else root / value


def preflight(root):
    branch = output(root, "symbolic-ref", "--quiet", "--short", "HEAD")
    if branch != "main":
        raise PullError("当前分支必须为 main；不自动切换分支")
    if (root / PENDING_JOURNAL).exists():
        raise PullError("存在未完成的库存事务；本工具不继续入账或覆盖事务")
    for name in ("MERGE_HEAD", "REBASE_HEAD", "rebase-apply", "rebase-merge", "CHERRY_PICK_HEAD", "REVERT_HEAD", "sequencer", "index.lock"):
        if git_path(root, name).exists():
            raise PullError(f"存在未完成或正在运行的 Git 操作：{name}")
    for args in (("diff", "--quiet", "--ignore-submodules=none"), ("diff", "--cached", "--quiet", "--ignore-submodules=none")):
        if git(root, *args, accepted=(0, 1)).returncode:
            raise PullError("存在已跟踪文件修改或暂存内容；完整保留，停止自动拉取")
    return output(root, "rev-parse", "HEAD")


def unique(pattern, text, label):
    matches = list(re.finditer(pattern, text, re.M))
    if len(matches) != 1:
        raise PullError(f"{label} 格式不正确或不唯一")
    return matches[0]


def count(text):
    if not re.fullmatch(r"(?:\d+|\d{1,3}(?:,\d{3})+)", text):
        raise PullError("库存数字格式无效")
    return int(text.replace(",", ""))


def validate_inventory(main, project=None):
    # The main ledger is authoritative. Keep the optional argument for callers
    # using the old API; navigation summaries are checked separately below.
    main = main.replace("\r\n", "\n")
    # Bare counts are valid only when the ledger explicitly declares pairs.
    preamble = main.split("## 当前汇总", 1)[0]
    pairs_declared = re.search(r"数量单位(?:统一)?(?:为|[：:])\s*[“\"「]?双", preamble)
    quantity = r"([\d,]+)(?: 双)?" if pairs_declared else r"([\d,]+) 双"

    def summary_count(label_pattern, label):
        # Count labels before parsing values, so an invalid duplicate cannot
        # disappear from a numeric-only regex and bypass uniqueness checks.
        cell = unique(r"^\| " + label_pattern + r" \| ([^|\n]*) \|$", main, label).group(1)
        value = re.fullmatch(quantity, cell.strip())
        if not value:
            raise PullError(f"{label} 数字或单位无效")
        return count(value.group(1))
    rows = list(re.finditer(
        r"^\| \d+ \| ([^|]+) \| ([^|]+) \| 成品 \| " + quantity + r" \| \d{4}-\d{2}-\d{2} \| [^\n]* \|$",
        main, re.M,
    ))
    quantities = {}
    for row in rows:
        color = row.group(1).strip()
        if not color or color in quantities:
            raise PullError("库存颜色为空或重复")
        quantities[color] = count(row.group(3))
    numbered_rows = re.findall(r"^\| \d+ \|", main, re.M)
    if not rows or len(rows) != len(numbered_rows) or len(quantities) != len(rows):
        raise PullError("必须提供不重复的非负成品库存明细")
    total = summary_count(r"(?:已录入(?:成品)?库存|当前成品库存)", "主记录汇总")
    color_count = unique(r"^\| 已录入颜色数 \| (\d+) 个 \|$", main, "颜色汇总").group(1)
    if color_count != str(len(quantities)) or total != sum(quantities.values()):
        raise PullError("主账颜色明细与成品汇总不一致")
    remaining = summary_count("剩余未包装半成品", "剩余未包装半成品")
    combined = summary_count("当前账面总库存（半成品＋成品）", "账面总库存")
    packaged = summary_count("累计已包装数量", "累计已包装数量")
    if total + remaining != combined:
        raise PullError("主账成品加剩余半成品与账面总库存不一致")
    date = unique(r"^更新日期：(\d{4}-\d{2}-\d{2})$", main, "更新日期").group(1)
    return {"total": total, "colors": quantities, "updated_at": date,
            "remaining": remaining, "combined": combined, "packaged": packaged}


def summary_warnings(inventory, project, inventory_readme="", index=""):
    """A missing/stale navigation summary never overrides a valid ledger."""
    checks = (
        (r"已录入 \d+ 个颜色，共 ([\d,]+) 双", "total", "成品合计"),
        (r"剩余未包装半成品 ([\d,]+) 双", "remaining", "未包装半成品"),
        (r"账面合计 ([\d,]+) 双", "combined", "账面合计"),
    )
    warnings = []
    for pattern, key, label in checks:
        try:
            value = count(unique(pattern, project, label).group(1))
            if value == inventory[key]:
                continue
        except PullError:
            pass
        warnings.append(f"项目摘要{label}待补齐；库存以主账为准，不影响接收")
    date_checks = (
        (project, r"账面合计 [\d,]+ 双（(\d{4}-\d{2}-\d{2})）", inventory["updated_at"], "项目摘要更新日期"),
        (inventory_readme, r"包含自 \d{4}-\d{2}-\d{2} 至 (\d{4}-\d{2}-\d{2}) 的更新内容", inventory["updated_at"], "库存目录更新日期"),
        (index, r"最近更新于 (\d{4}-\d{2}-\d{2})，", inventory["updated_at"], "索引更新日期"),
    )
    for text, pattern, expected, label in date_checks:
        if not text:
            continue
        try:
            value = unique(pattern, text, label).group(1)
            if value == expected:
                continue
        except PullError:
            pass
        warnings.append(f"{label}待补齐；库存以主账为准，不影响接收")
    index_checks = (
        (r"成品合计 ([\d,]+) 双", "total", "索引成品合计"),
        (r"剩余未包装半成品 ([\d,]+) 双", "remaining", "索引未包装半成品"),
        (r"账面合计 ([\d,]+) 双", "combined", "索引账面合计"),
    )
    for pattern, key, label in index_checks:
        if not index:
            continue
        try:
            value = count(unique(pattern, index, label).group(1))
            if value == inventory[key]:
                continue
        except PullError:
            pass
        warnings.append(f"{label}待补齐；库存以主账为准，不影响接收")
    return warnings


def candidate_file(root, commit, path, optional=False):
    entry = output(root, "ls-tree", commit, "--", path)
    if not entry and optional:
        return ""
    if not entry.startswith(("100644 blob ", "100755 blob ")):
        raise PullError(f"候选库存文件不存在或不是普通文件：{path}")
    blob = entry.split()[2]
    try:
        return git(root, "cat-file", "blob", blob).stdout.decode("utf-8")
    except UnicodeError as exc:
        raise PullError(f"候选文件不是 UTF-8 文本：{path}") from exc


def check_untracked_collisions(root, before, candidate):
    changed = paths(root, "diff", "--name-only", "--diff-filter=ACMRT", "-z", before, candidate)
    # Include ignored files: Git otherwise permits overwriting some ignored paths.
    untracked = paths(root, "ls-files", "--others", "-z")
    for incoming in changed:
        for local in untracked:
            if incoming == local or incoming.startswith(local + "/") or local.startswith(incoming + "/"):
                raise PullError(f"远端变更会碰到电脑未跟踪文件，已保留：{local}")
        target = root / incoming
        if target.is_dir() and not output(root, "ls-files", "--", incoming):
            raise PullError(f"远端文件会替换电脑现有目录，已保留：{incoming}")


def save_state(path, result):
    state = dict(result, checked_at=datetime.now(timezone.utc).isoformat())
    fd, temporary = tempfile.mkstemp(prefix=".mobile-github-pull-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(state, ensure_ascii=False, indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def receive(root, test_local_remote=False):
    root = root.resolve()
    if output(root, "rev-parse", "--show-toplevel") != str(root):
        raise PullError("--root 必须准确指向仓库根目录")
    validate_remote(root, test_local_remote)
    gitdir = Path(output(root, "rev-parse", "--absolute-git-dir"))
    with (gitdir / LOCK_NAME).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            before = preflight(root)
            # Fetch only main; check ancestry before changing the working tree.
            git(root, "fetch", "--no-tags", "--no-recurse-submodules", "origin",
                "refs/heads/main:refs/remotes/origin/main", test_local=test_local_remote)
            candidate = output(root, "rev-parse", "refs/remotes/origin/main")
            if preflight(root) != before:
                raise PullError("检查期间电脑 HEAD 已变化，停止本轮同步")
            if git(root, "merge-base", "--is-ancestor", before, candidate, accepted=(0, 1)).returncode:
                raise PullError("电脑分支领先或与远端分叉；不自动合并、变基或覆盖")
            expected = validate_inventory(candidate_file(root, candidate, MAIN))
            project = candidate_file(root, candidate, PROJECT, optional=True)
            inventory_readme = candidate_file(root, candidate, INVENTORY_README, optional=True)
            index = candidate_file(root, candidate, INDEX, optional=True)
            warnings = summary_warnings(expected, project, inventory_readme, index)
            if before != candidate:
                check_untracked_collisions(root, before, candidate)
                if preflight(root) != before:
                    raise PullError("快进前电脑内容发生变化，停止本轮同步")
                git(root, "merge", "--ff-only", "--no-edit", "--no-overwrite-ignore", candidate)
            actual_head = output(root, "rev-parse", "HEAD")
            actual = validate_inventory((root / MAIN).read_text(encoding="utf-8"))
            actual_project = (root / PROJECT).read_bytes().decode("utf-8") if (root / PROJECT).exists() else ""
            if actual_head != candidate or actual != expected or preflight(root) != candidate:
                raise PullError("拉取后核验不一致；保留当前内容并报告，绝不自动回滚")
            if actual_project != project:
                raise PullError("接收后摘要文件与远端不一致；保留当前内容并报告")
            result = {
                "status": "unchanged" if before == candidate else "fast_forwarded",
                "before_commit": before, "commit": candidate, "inventory": actual,
                "mode": "github_primary_computer_receive_only",
                "warnings": warnings,
            }
            save_state(gitdir / STATE_NAME, result)
            return result
        except (PullError, OSError, UnicodeError) as exc:
            save_state(gitdir / STATE_NAME, {"status": "blocked", "error": str(exc)})
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/Users/xianer/Desktop/贤二Ai大脑知识库"))
    parser.add_argument("--test-local-remote", action="store_true", help="仅供临时 fixture：允许本地 bare origin")
    args = parser.parse_args()
    try:
        result = receive(args.root, args.test_local_remote)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (PullError, OSError, UnicodeError) as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
