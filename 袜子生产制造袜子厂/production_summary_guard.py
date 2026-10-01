#!/usr/bin/env python3
"""Validate that the published daily summary matches the detail records.

The production ledger keeps one detail block per worker and production date,
while the daily summary is a derived view near the top of the same Markdown
file.  This module makes that relationship executable so a successful GitHub
write cannot silently leave the daily view behind.
"""

from __future__ import annotations

import argparse
import re
from collections import defaultdict
from pathlib import Path


WORKERS = {
    "A": ("徐超超", "下机"),
    "B": ("梅芳", "下机"),
    "C": ("李鸿玉", "烤边"),
    "D": ("张小翠", "烤边"),
}
PRODUCTS = ("棉堆堆袜", "冰冰袜", "小腿袜", "过膝袜", "女船袜", "男船袜")

DETAIL_ROW = re.compile(
    r"^\| (20\d{6}-[ABCD]-\d{3}) \| ([^|]+) \| (\d+) \| ([^|]+) \|$",
    re.M,
)
DAY_HEADING = re.compile(r"^### (20\d{2}-\d{2}-\d{2})｜[^\n]+$", re.M)
SUMMARY_HEADING = re.compile(r"^#### (20\d{2}-\d{2}-\d{2})\s*$", re.M)
SUMMARY_ROW = re.compile(
    r"^\| ([ABCD]｜[^|]+) \| ([^|]+) \| ([^|]+) \| ([^|]+) \| "
    r"([^|]+) \| ([^|]+) \| ([^|]+) \| ([^|]+) \| ([^|]+) \|$",
    re.M,
)


def _section(text: str, code: str) -> str:
    start = text.find(f"## {code}｜")
    if start < 0:
        raise ValueError(f"缺少 {code} 个人生产记录区")
    end = text.find("\n## ", start + 3)
    return text[start : end if end >= 0 else None]


def detail_totals(text: str) -> dict[tuple[str, str], dict[str, dict[str, int]]]:
    """Return numeric detail totals keyed by (date, worker).

    A product omitted from a detail table is intentionally absent, which is
    different from an explicit zero.  Rows marked revoked are excluded from
    the current view but remain available for audit in the ledger.
    """

    totals: dict[tuple[str, str], dict[str, dict[str, int]]] = defaultdict(
        lambda: defaultdict(dict)
    )
    seen: set[str] = set()

    for code in WORKERS:
        section = _section(text, code)
        headings = list(DAY_HEADING.finditer(section))
        for index, heading in enumerate(headings):
            date = heading.group(1)
            end = headings[index + 1].start() if index + 1 < len(headings) else len(section)
            body = section[heading.end() : end]
            for record_id, product, quantity, status in DETAIL_ROW.findall(body):
                if record_id in seen:
                    raise ValueError(f"重复生产记录编号：{record_id}")
                seen.add(record_id)
                product = product.strip()
                status = status.strip()
                if product not in PRODUCTS:
                    raise ValueError(f"{record_id} 使用了未知产品：{product}")
                if "撤销" in status or "作废" in status:
                    continue
                record_code = record_id.split("-")[1]
                if record_code != code:
                    raise ValueError(f"{record_id} 出现在错误的 {code} 个人区")
                current = totals[(date, code)]
                if product in current:
                    raise ValueError(f"{date} {code} {product} 有重复有效明细")
                current[product] = int(quantity)
    return totals


def summary_rows(text: str) -> dict[tuple[str, str], tuple[str, ...]]:
    """Return daily summary rows keyed by (date, worker)."""

    start = text.find("### 2026年9月每日汇总")
    if start < 0:
        raise ValueError("缺少 2026年9月每日汇总区")
    # Daily summaries span the September-to-December month blocks.  The old
    # boundary stopped at the October heading, so a valid 10/01 detail row was
    # falsely reported as missing from the daily summary.
    end = text.find("\n## 七、", start)
    section = text[start : end if end >= 0 else None]
    headings = list(SUMMARY_HEADING.finditer(section))
    rows: dict[tuple[str, str], tuple[str, ...]] = {}
    for index, heading in enumerate(headings):
        date = heading.group(1)
        block_end = headings[index + 1].start() if index + 1 < len(headings) else len(section)
        body = section[heading.end() : block_end]
        for match in SUMMARY_ROW.finditer(body):
            worker_label = match.group(1).strip()
            code, name = worker_label.split("｜", 1)
            if code not in WORKERS or WORKERS[code][0] != name:
                raise ValueError(f"{date} 每日汇总人员映射异常：{worker_label}")
            key = (date, code)
            if key in rows:
                raise ValueError(f"{date} 每日汇总重复人员：{worker_label}")
            rows[key] = tuple(part.strip() for part in match.groups()[1:])
    return rows


def check_daily_summary(text: str) -> None:
    """Raise ``ValueError`` when any published daily row is stale or missing."""

    details = detail_totals(text)
    published = summary_rows(text)
    detail_keys = set(details)
    published_keys = set(published)

    missing = sorted(detail_keys - published_keys)
    if missing:
        date, code = missing[0]
        name = WORKERS[code][0]
        raise ValueError(f"{date} 每日汇总缺少 {code}｜{name} 行")

    unexpected = sorted(published_keys - detail_keys)
    for date, code in unexpected:
        values = published[(date, code)]
        if all(value in {"—", "-", ""} for value in values[1:]):
            continue
        name = WORKERS[code][0]
        raise ValueError(f"{date} 每日汇总存在无明细的 {code}｜{name} 行")

    for key in sorted(detail_keys):
        actual = published[key]
        code = key[1]
        detail = details[key]
        if actual[0] != WORKERS[code][1]:
            date, code = key
            name = WORKERS[code][0]
            raise ValueError(f"{date} {code}｜{name} 工序不一致：显示 {actual[0]}")
        for index, product in enumerate(PRODUCTS, start=1):
            if product in detail:
                expected = str(detail[product])
                if actual[index] != expected:
                    date, code = key
                    name = WORKERS[code][0]
                    raise ValueError(
                        f"{date} {code}｜{name} 每日汇总不一致："
                        f"{product} 显示 {actual[index]}，应为 {expected}"
                    )
            elif actual[index] not in {"0", "核实"}:
                date, code = key
                name = WORKERS[code][0]
                raise ValueError(
                    f"{date} {code}｜{name} 每日汇总不一致："
                    f"{product} 显示 {actual[index]}，应为 0 或 核实"
                )
        expected_subtotal = str(sum(detail.values()))
        if actual[-1] != expected_subtotal:
            date, code = key
            name = WORKERS[code][0]
            raise ValueError(
                f"{date} {code}｜{name} 每日汇总不一致："
                f"合计显示 {actual[-1]}，应为 {expected_subtotal}"
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ledger", type=Path, help="Markdown production ledger")
    args = parser.parse_args()
    check_daily_summary(args.ledger.read_text(encoding="utf-8"))
    print(f"每日汇总校验通过：{args.ledger}")


if __name__ == "__main__":
    main()
