#!/usr/bin/env python3
"""检查生产主账新增入账是否丢失旧记录或写错汇总。

用法：python3 guard_production_ledger.py 旧版.md 候选版.md

校验按候选台账中的 ``YYYY年M月个人累计``、年度/半年度累计区块
自动识别期间，不再把某个固定月份写死。更正、撤销须由人工另行审核，
不能用普通新增入口绕过本检查。
"""
from __future__ import annotations

import calendar
import re
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterable

WORKERS = {"A": "徐超超", "B": "梅芳", "C": "李鸿玉", "D": "张小翠"}
PRODUCTS = ("棉堆堆袜", "冰冰袜", "小腿袜", "过膝袜", "女船袜", "男船袜")
# A row may retain 核实 for an unreported product. It is preserved, but not
# counted as a numeric quantity until a subsequent correction supplies a number.
ROW = re.compile(
    r"^\| (20\d{6}-[ABCD]-\d{3}) \| ([^|]+) \| (\d+|核实) \| ([^|]+) \|$",
    re.M,
)
MESSAGE = re.compile(r"^(?:来源消息|确认消息)：(om_[a-zA-Z0-9]+)$", re.M)
MONTH_HEADING = re.compile(
    r"^### ([ABCD])｜(20\d{2})年(\d{1,2})月个人累计\s*$", re.M
)
ANNUAL_HEADING = re.compile(
    r"^## (?:[^\n]*?)(20\d{2})年([^\n]*?)(?:累计汇总|年度汇总)[^\n]*$", re.M
)
WORKER_HEADING = re.compile(r"^### ([ABCD])组｜([^\n]+)$", re.M)


@dataclass(frozen=True)
class Record:
    product: str
    quantity: int | None
    status: str
    code: str
    record_date: date


def detail(text: str, code: str, required: bool = True) -> str:
    start = text.find("## " + code + "｜")
    if start < 0:
        if required:
            raise ValueError(f"缺少 {code} 个人区")
        return ""
    end = text.find("\n## ", start + 3)
    return text[start : end if end >= 0 else None]


def _parse_record_date(key: str) -> date:
    raw = key[:8]
    return date(int(raw[:4]), int(raw[4:6]), int(raw[6:8]))


def records(text: str) -> dict[str, Record]:
    """Parse detail rows while retaining 核实 rows and revoked history."""

    result: dict[str, Record] = {}
    for code in WORKERS:
        # A newly registered worker may be absent from the pre-change baseline.
        section = detail(text, code, required=False)
        for key, product, quantity, status in ROW.findall(section):
            product = product.strip()
            status = status.strip()
            if key in result:
                raise ValueError(f"重复记录编号：{key}")
            if key.split("-")[1] != code or product not in PRODUCTS:
                raise ValueError(f"记录归属或产品异常：{key}")
            result[key] = Record(
                product=product,
                quantity=None if quantity == "核实" else int(quantity),
                status=status,
                code=code,
                record_date=_parse_record_date(key),
            )
    return result


def _effective(value: Record) -> bool:
    return "撤销" not in value.status and "作废" not in value.status


def _period_totals(rows: Iterable[Record], start: date, end: date) -> dict[str, dict[str, int]]:
    result = {code: {product: 0 for product in PRODUCTS} for code in WORKERS}
    for row in rows:
        if row.record_date < start or row.record_date > end or not _effective(row):
            continue
        if row.quantity is not None:
            result[row.code][row.product] += row.quantity
    return result


def _read_product_table(section: str, label: str | tuple[str, ...]) -> tuple[dict[str, str], int]:
    """Read product/value rows and the personal subtotal from a month block."""
    shown: dict[str, str] = {}
    for product in PRODUCTS:
        matches = re.findall(
            r"^\| " + re.escape(product) + r" \| ([^|]+) \|", section, re.M
        )
        if len(matches) != 1:
            raise ValueError(f"{label} {product} 个人累计缺失或重复")
        shown[product] = matches[0].strip()
    labels = (label,) if isinstance(label, str) else tuple(label)
    label_pattern = "(?:" + "|".join(re.escape(item) for item in labels) + ")"
    matches = re.findall(r"^\| " + label_pattern + r" \| (\d+) \|", section, re.M)
    if len(matches) != 1:
        raise ValueError(f"{'/'.join(labels)} 个人小计缺失或重复")
    return shown, int(matches[0])


def _check_shown(
    *, code: str, name: str, shown: dict[str, str], expected: dict[str, int], period: str
) -> int:
    subtotal = 0
    for product in PRODUCTS:
        value = shown[product]
        amount = expected[product]
        if value == "核实":
            # A 核实 marker is valid only if the period has no positive numeric
            # quantity for this product; it represents an unresolved report.
            if amount:
                raise ValueError(
                    f"{name} {period} {product} 仍标核实，但已有已报数量 {amount}"
                )
            continue
        if not value.isdecimal() or int(value) != amount:
            raise ValueError(
                f"{name} {period} {product} 显示 {value}，与明细 {amount} 不符"
            )
        subtotal += int(value)
    return subtotal


def _month_end(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def check_month_blocks(text: str, rows: dict[str, Record]) -> None:
    """Validate every per-worker monthly block found in the candidate ledger."""
    seen_blocks: set[tuple[str, int, int]] = set()
    for match in MONTH_HEADING.finditer(text):
        code, year_raw, month_raw = match.groups()
        year, month = int(year_raw), int(month_raw)
        if not 1 <= month <= 12:
            raise ValueError(f"{code} 个人累计月份异常：{year}年{month}月")
        block_start = match.end()
        block_key = (code, year, month)
        if block_key in seen_blocks:
            raise ValueError(f"{WORKERS[code]} {year}年{month}月个人累计重复")
        seen_blocks.add(block_key)
        next_heading = MONTH_HEADING.search(text, block_start)
        next_section = text.find("\n## ", block_start)
        ends = [pos for pos in (next_heading.start() if next_heading else -1, next_section) if pos >= 0]
        block_end = min(ends) if ends else len(text)
        block = text[block_start:block_end]
        labels = {
            "A": ("A已报小计", "A个人小计"),
            "B": ("B已报小计", "B个人小计"),
            "C": ("C已报小计", "C个人小计"),
            "D": ("D已报小计", "D个人小计"),
        }[code]
        shown, subtotal = _read_product_table(block, labels)
        expected_all = _period_totals(rows.values(), date(year, month, 1), _month_end(year, month))
        expected = expected_all[code]
        calculated = _check_shown(
            code=code,
            name=WORKERS[code],
            shown=shown,
            expected=expected,
            period=f"{year}年{month}月",
        )
        if subtotal != calculated:
            raise ValueError(
                f"{WORKERS[code]} {year}年{month}月个人小计 {subtotal} 与明细 {calculated} 不符"
            )

    # A newly appended detail row must have a matching month block. This is
    # the guard that prevents a successful upload from silently leaving the
    # October/January personal cumulative section behind.
    required_blocks = {
        (row.code, row.record_date.year, row.record_date.month)
        for row in rows.values()
        if _effective(row)
    }
    missing = sorted(required_blocks - seen_blocks)
    if missing:
        code, year, month = missing[0]
        raise ValueError(f"{WORKERS[code]} 缺少 {year}年{month}月个人累计")


def _annual_range(year: int, descriptor: str) -> tuple[date, date]:
    if "下半年" in descriptor:
        return date(year, 7, 1), date(year, 12, 31)
    if "上半年" in descriptor:
        return date(year, 1, 1), date(year, 6, 30)
    if "季度" in descriptor:
        m = re.search(r"第?([一二三四1234])季度", descriptor)
        if m:
            q = {"一": 1, "二": 2, "三": 3, "四": 4}.get(m.group(1), int(m.group(1)) if m.group(1).isdigit() else 1)
            return date(year, (q - 1) * 3 + 1, 1), _month_end(year, q * 3)
    return date(year, 1, 1), date(year, 12, 31)


def check_annual_blocks(text: str, rows: dict[str, Record]) -> None:
    """Validate numbered/current annual summary tables in any supported year.

    Historical ``<details>`` appendices are intentionally ignored; they remain
    immutable audit material. Current blocks use ``### A组｜姓名`` headings and
    product rows, the same shape used by the 2026 ledger and 2027 template.
    """
    for heading in ANNUAL_HEADING.finditer(text):
        # Avoid re-validating the historical appendix, which is explicitly a
        # trace-only snapshot and can contain pre-revision values.
        before = text[max(0, heading.start() - 80): heading.start()]
        if "<summary>" in before or "历史" in before:
            continue
        year, descriptor = int(heading.group(1)), heading.group(2)
        start, end = _annual_range(year, descriptor)
        section_start = heading.end()
        next_h = re.search(r"^## ", text[section_start:], re.M)
        section_end = section_start + next_h.start() if next_h else len(text)
        section = text[section_start:section_end]
        worker_matches = list(WORKER_HEADING.finditer(section))
        if not worker_matches:
            continue
        expected_all = _period_totals(rows.values(), start, end)
        for index, worker_match in enumerate(worker_matches):
            code = worker_match.group(1)
            worker_end = worker_matches[index + 1].start() if index + 1 < len(worker_matches) else len(section)
            worker_section = section[worker_match.end():worker_end]
            shown = {}
            for product in PRODUCTS:
                matches = re.findall(
                    r"^\| " + re.escape(product) + r" \| ([^|]+) \|", worker_section, re.M
                )
                if len(matches) != 1:
                    raise ValueError(f"{year}年累计 {WORKERS[code]} {product} 缺失或重复")
                shown[product] = matches[0].strip()
            # Different ledgers use X已报小计 or just a bold worker subtotal.
            subtotal_matches = re.findall(
                r"^\| \*\*?(?:" + re.escape(code) + r"[^|]*小计)\*\*? \| \*\*?(\d+)\*\*? \|",
                worker_section,
                re.M,
            )
            subtotal = int(subtotal_matches[0]) if subtotal_matches else None
            calculated = _check_shown(
                code=code,
                name=WORKERS[code],
                shown=shown,
                expected=expected_all[code],
                period=f"{year}年{descriptor.strip()}",
            )
            if subtotal is not None and subtotal != calculated:
                raise ValueError(
                    f"{WORKERS[code]} {year}年{descriptor.strip()}小计 {subtotal} 与明细 {calculated} 不符"
                )


def check(before: str, after: str) -> None:
    previous, current = records(before), records(after)
    for key, value in previous.items():
        if key not in current:
            raise ValueError(f"旧记录被删除：{key}")
        if current[key] != value:
            raise ValueError(f"旧记录被改动：{key}")
    lost_messages = set(MESSAGE.findall(before)) - set(MESSAGE.findall(after))
    if lost_messages:
        raise ValueError("来源/确认消息丢失：" + ", ".join(sorted(lost_messages)))
    check_month_blocks(after, current)
    check_annual_blocks(after, current)
    print(f"校验通过：保留旧记录 {len(previous)} 条，现有 {len(current)} 条，个人/月度累计吻合")


if __name__ == "__main__":
    try:
        if len(sys.argv) != 3:
            raise ValueError("用法：guard_production_ledger.py 旧版.md 候选版.md")
        check(Path(sys.argv[1]).read_text(encoding="utf-8"), Path(sys.argv[2]).read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        print(f"生产台账校验失败：{exc}", file=sys.stderr)
        sys.exit(1)
