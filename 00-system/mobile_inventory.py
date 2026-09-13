#!/usr/bin/env python3
"""Apply one verified mobile inventory message, with durable retry protection.

The caller must verify message authorship, intent, and the activation baseline.
This tool does not send chat messages, commit files, or push to GitHub.
"""

import argparse
from contextlib import contextmanager
from datetime import date as calendar_date
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile


THREAD_ID = "6aa6ce99-38c4-83ea-80c2-51438053f9b4"
COLORS = {"漂白", "黑色", "荧光紫", "水龙卷", "青绿", "鲜紫"}
MAIN = "袜子生产制造袜子厂/库存记录/2026下半年冰冰袜库存包装统计.md"
PROJECT = "袜子生产制造袜子厂/README.md"
INVENTORY_INDEX = "袜子生产制造袜子厂/库存记录/README.md"
INDEX = "03-index/index.md"
STATE = "00-system/mobile-inventory-state.json"
JOURNAL = "00-system/.mobile-inventory-journal.json"
BUSINESS_FILES = (MAIN, PROJECT, INVENTORY_INDEX, INDEX)
RECEIPT_MARKERS = (
    "[库存同步回执]", "【电脑库存核验回执｜", "电脑同步回执",
    "库存同步测试", "库存同步确认", "同步测试回执",
)
ROW = re.compile(
    r"^\| (\d+) \| ([^|]+) \| ([^|]+) \| ([^|]+) \| "
    r"([\d,]+) 双 \| (\d{4}-\d{2}-\d{2}) \| (.*?) \|$", re.M
)


class InventoryError(ValueError):
    pass


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read(root, relative):
    try:
        return (root / relative).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise InventoryError(f"无法读取 {relative}: {exc}") from exc


def load_json(text, label):
    try:
        return json.loads(text)
    except (ValueError, TypeError) as exc:
        raise InventoryError(f"{label} 不是有效 JSON") from exc


def json_text(value):
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def atomic_write(path, text):
    """Replace one file and flush both its bytes and directory entry."""
    mode = (path.stat().st_mode & 0o777) if path.exists() else 0o600
    fd, temporary = tempfile.mkstemp(prefix=".mobile-inventory-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        flush_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def flush_directory(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextmanager
def locked(root):
    runtime = Path(tempfile.gettempdir()) / f"codex-mobile-inventory-{os.getuid()}"
    runtime.mkdir(mode=0o700, exist_ok=True)
    lock = runtime / (digest(str(root)) + ".lock")
    with lock.open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def one(pattern, text, label):
    matches = list(re.finditer(pattern, text, re.M))
    if len(matches) != 1:
        raise InventoryError(f"{label} 应恰好出现一次，实际 {len(matches)} 次")
    return matches[0]


def number(text):
    if not re.fullmatch(r"(?:\d+|\d{1,3}(?:,\d{3})+)", text):
        raise InventoryError("库存数字格式不正确")
    return int(text.replace(",", ""))


def parse_inventory(text):
    matches = list(ROW.finditer(text))
    rows = {}
    for match in matches:
        color = match.group(2)
        if color in rows or color not in COLORS or match.group(4) != "成品":
            raise InventoryError("库存明细不是预期的六种颜色成品库存")
        rows[color] = {"quantity": number(match.group(5)), "match": match}
    if set(rows) != COLORS:
        raise InventoryError("库存明细缺少颜色，或表格格式已经改变")
    summary = one(r"^\| 已录入库存 \| ([\d,]+) 双 \|$", text, "库存汇总")
    total = number(summary.group(1))
    if total != sum(row["quantity"] for row in rows.values()):
        raise InventoryError("库存汇总与六种颜色明细不一致，停止入账")
    count = one(r"^\| 已录入颜色数 \| (\d+) 个 \|$", text, "颜色汇总")
    if count.group(1) != "6":
        raise InventoryError("颜色汇总不是 6 个")
    updated = one(r"^更新日期：(\d{4}-\d{2}-\d{2})$", text, "更新日期")
    return rows, total, updated.group(1)


def snapshot(root):
    rows, total, updated = parse_inventory(read(root, MAIN))
    return {
        "updated_at": updated, "total": total,
        "colors": {color: row["quantity"] for color, row in rows.items()},
        "pending_transaction": (root / JOURNAL).exists(),
    }


def validate_command(command):
    keys = {"thread_id", "message_id", "source_text", "color", "operation", "quantity", "date"}
    if not isinstance(command, dict) or set(command) != keys:
        raise InventoryError("指令字段必须为 thread_id, message_id, source_text, color, operation, quantity, date")
    if command["thread_id"] != THREAD_ID:
        raise InventoryError("此聊天不在手机库存同步白名单中")
    if not isinstance(command["message_id"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", command["message_id"]):
        raise InventoryError("message_id 无效")
    source = command["source_text"]
    if not isinstance(source, str) or not source.strip() or len(source) > 4000:
        raise InventoryError("source_text 必须是完整且简短的用户库存指令")
    if any(marker in source for marker in RECEIPT_MARKERS):
        raise InventoryError("系统回执或同步测试不能作为库存指令")
    if not isinstance(command["color"], str) or not isinstance(command["operation"], str) or command["color"] not in COLORS or command["operation"] not in {"add", "subtract", "set"}:
        raise InventoryError("仅支持已有六种颜色，以及 add/subtract/set")
    if type(command["quantity"]) is not int or command["quantity"] < 0:
        raise InventoryError("quantity 必须是非负整数，单位为双")
    if not isinstance(command["date"], str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", command["date"]):
        raise InventoryError("date 必须为 YYYY-MM-DD")
    try:
        calendar_date.fromisoformat(command["date"])
    except ValueError as exc:
        raise InventoryError("date 不是有效日期") from exc


def replace_one(pattern, replacement, text, label):
    match = one(pattern, text, label)
    value = replacement(match) if callable(replacement) else replacement
    return text[:match.start()] + value + text[match.end():]


def build_transaction(root, command, state, state_before):
    originals = {path: read(root, path) for path in BUSINESS_FILES}
    main = originals[MAIN]
    rows, old_total, updated = parse_inventory(main)
    if command["date"] < updated:
        raise InventoryError("指令日期早于主记录更新日期，请核实后再入账")
    before = rows[command["color"]]["quantity"]
    amount = command["quantity"]
    operation = command["operation"]
    after = {"add": before + amount, "subtract": before - amount, "set": amount}[operation]
    if after < 0:
        raise InventoryError("本次减少会使库存为负数，停止入账")
    total = old_total + after - before
    transaction_id = f"mobile:{THREAD_ID}:{command['message_id']}"
    if f"`{transaction_id}`" in main:
        raise InventoryError("主记录已有此事务但账本缺失；停止并核对，避免重复入账")
    action = {"add": "增加", "subtract": "减少", "set": "设为"}[operation]
    description = f"{command['color']}成品{action} {amount:,} 双"
    row = rows[command["color"]]["match"]
    new_row = (
        f"| {row.group(1)} | {command['color']} | {row.group(3)} | 成品 | "
        f"{after:,} 双 | {command['date']} | 手机用户指令录入，本次{action} {amount:,} 双 |"
    )
    main = main[:row.start()] + new_row + main[row.end():]
    main = replace_one(r"^更新日期：\d{4}-\d{2}-\d{2}$", f"更新日期：{command['date']}", main, "更新日期")
    main = replace_one(r"^\| 已录入库存 \| [\d,]+ 双 \|$", f"| 已录入库存 | {total:,} 双 |", main, "库存汇总")
    source = " ".join(command["source_text"].split()).replace("|", "\\|")
    history = (
        f"| {command['date']} | 手机指令“{source}”；{command['color']}成品由 {before:,} 双"
        f"调整至 {after:,} 双；事务 `{transaction_id}` | 总库存 {total:,} 双，6 个颜色 |\n"
    )
    history_section = re.search(r"(^## 更新记录\n\n)(.*?)(?=^## |\Z)", main, re.M | re.S)
    if not history_section or not history_section.group(2).rstrip().endswith("|"):
        raise InventoryError("更新记录表格格式已经改变")
    end = history_section.start(2) + len(history_section.group(2).rstrip())
    main = main[:end] + "\n" + history.rstrip() + main[end:]
    parse_inventory(main)
    project_match = one(r"已录入 6 个颜色，共 ([\d,]+) 双", originals[PROJECT], "项目汇总")
    if number(project_match.group(1)) != old_total:
        raise InventoryError("袜子项目 README 汇总与主记录不一致")
    replacements = {
        MAIN: main,
        PROJECT: replace_one(r"已录入 6 个颜色，共 [\d,]+ 双", f"已录入 6 个颜色，共 {total:,} 双", originals[PROJECT], "项目汇总"),
        INVENTORY_INDEX: replace_one(
            r"包含自 (\d{4}-\d{2}-\d{2}) 至 \d{4}-\d{2}-\d{2} 的更新内容",
            lambda m: f"包含自 {m.group(1)} 至 {command['date']} 的更新内容", originals[INVENTORY_INDEX], "库存索引日期"),
        INDEX: replace_one(
            r"^(\- \[冰冰袜库存主记录\]\([^\n]+\))：[^\n]+$",
            lambda m: f"{m.group(1)}：最近更新于 {command['date']}，{description}，已录入总库存 {total:,} 双。",
            originals[INDEX], "知识库库存入口"),
    }
    event = {
        "transaction_id": transaction_id, "source": dict(command),
        "before": {"color_quantity": before, "total": old_total},
        "after": {"color_quantity": after, "total": total},
        "status": "applied", "github_status": "pending", "receipt_status": "pending",
        "files": list(BUSINESS_FILES),
    }
    state["events"].append(event)
    observed = state.setdefault("observed_message_ids", [])
    if command["message_id"] not in observed:
        observed.append(command["message_id"])
    originals[STATE] = state_before
    replacements[STATE] = json_text(state)
    return {
        "version": 1, "transaction_id": transaction_id, "event": event,
        "files": [
            {"path": path, "before": originals[path], "after": text,
             "before_sha256": digest(originals[path]), "after_sha256": digest(text)}
            for path, text in replacements.items()
        ],
    }


def recover_locked(root):
    if not (root / JOURNAL).exists():
        return None
    journal = load_json(read(root, JOURNAL), "事务日志")
    if not isinstance(journal, dict):
        raise InventoryError("事务日志必须为 JSON 对象，停止恢复")
    files = journal.get("files")
    if (
        journal.get("version") != 1 or not isinstance(files, list)
        or not all(isinstance(item, dict) for item in files)
        or [item.get("path") for item in files] != [*BUSINESS_FILES, STATE]
        or not isinstance(journal.get("event"), dict)
    ):
        raise InventoryError("事务日志格式或文件范围不正确，停止恢复")
    pending = []
    for item in files:
        if not all(isinstance(item.get(key), str) for key in ("before", "after", "before_sha256", "after_sha256")):
            raise InventoryError("事务日志缺少完整文件内容或校验值，停止恢复")
        if digest(item["before"]) != item["before_sha256"] or digest(item["after"]) != item["after_sha256"]:
            raise InventoryError("事务日志内容校验失败，停止恢复")
        current = digest(read(root, item["path"]))
        if current not in {item["before_sha256"], item["after_sha256"]}:
            raise InventoryError(f"{item['path']} 在事务中断后被其他操作修改，停止恢复以保留改动")
        if current != item["after_sha256"]:
            pending.append(item)
    for item in pending:
        atomic_write(root / item["path"], item["after"])
    (root / JOURNAL).unlink()
    flush_directory((root / JOURNAL).parent)
    return journal["event"]


def apply(root, command):
    validate_command(command)
    with locked(root):
        recover_locked(root)
        state_before = read(root, STATE)
        state = load_json(state_before, "同步状态")
        if not isinstance(state, dict) or state.get("version") != 1 or state.get("thread_id") != THREAD_ID or not isinstance(state.get("events"), list):
            raise InventoryError("同步状态尚未正确初始化，拒绝入账")
        for event in state["events"]:
            source = event.get("source", {})
            if source.get("thread_id") == THREAD_ID and source.get("message_id") == command["message_id"]:
                if source != command:
                    raise InventoryError("同一消息 ID 的指令内容发生变化，拒绝重新入账")
                return {"duplicate": True, "event": event}
        if command["message_id"] in state.get("receipt_message_ids", []):
            raise InventoryError("电脑回执消息不能作为库存指令")
        if command["message_id"] in state.get("observed_message_ids", []):
            raise InventoryError("此消息已登记观察，不能作为新库存指令再次处理")
        journal = build_transaction(root, command, state, state_before)
        atomic_write(root / JOURNAL, json_text(journal))
        event = recover_locked(root)
        return {"duplicate": False, "event": event}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("command", choices=("snapshot", "apply", "recover"))
    parser.add_argument("--json", help="指令 JSON 文件；省略时从标准输入读取")
    args = parser.parse_args()
    root = args.root.resolve()
    try:
        if args.command == "apply":
            payload = Path(args.json).read_text(encoding="utf-8") if args.json else sys.stdin.read()
            result = apply(root, load_json(payload, "指令"))
        else:
            with locked(root):
                result = snapshot(root) if args.command == "snapshot" else {"recovered": recover_locked(root)}
        print(json_text(result), end="")
    except (InventoryError, OSError) as exc:
        print(json_text({"error": str(exc)}), file=sys.stderr, end="")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
