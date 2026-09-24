#!/usr/bin/env python3
"""检查生产主账新增入账是否丢失旧记录或写错个人累计。

用法：python3 guard_production_ledger.py 旧版.md 候选版.md
更正、撤销须由人工另行审核，不能用普通新增入口绕过本检查。
"""
import re
import sys
from pathlib import Path

WORKERS = {"A": "徐超超", "B": "梅芳", "C": "李鸿玉"}
PRODUCTS = ("棉堆堆袜", "冰冰袜", "小腿袜", "过膝袜", "女船袜", "男船袜")
ROW = re.compile(r"^\| (20\d{6}-[ABC]-\d{3}) \| ([^|]+) \| (\d+) \| ([^|]+) \|$", re.M)
MESSAGE = re.compile(r"^(?:来源消息|确认消息)：(om_[a-zA-Z0-9]+)$", re.M)


def detail(text, code):
    start = text.find("## " + code + "｜")
    if start < 0:
        raise ValueError(f"缺少 {code} 个人区")
    end = text.find("\n## ", start + 3)
    return text[start:end if end >= 0 else None]


def records(text):
    result = {}
    for code in WORKERS:
        for key, product, qty, status in ROW.findall(detail(text, code)):
            if key in result:
                raise ValueError(f"重复记录编号：{key}")
            if key.split("-")[1] != code or product.strip() not in PRODUCTS:
                raise ValueError(f"记录归属或产品异常：{key}")
            result[key] = (product.strip(), int(qty), status.strip(), code)
    return result


def totals(text, rows):
    for code, name in WORKERS.items():
        section = detail(text, code)
        month = re.search(r"### " + code + r"｜2026年9月个人累计(.*?)(?=\n## |\Z)", section, re.S)
        if not month:
            raise ValueError(f"{name} 缺少个人累计")
        lines = month.group(1).splitlines()
        sums = {p: sum(value[1] for key, value in rows.items()
                       if value[3] == code and value[0] == p and key.startswith("202609"))
                for p in PRODUCTS}
        for product, amount in sums.items():
            matches = [m.group(1).strip() for line in lines
                       if (m := re.match(r"^\| " + re.escape(product) + r" \| ([^|]+) \|", line))]
            if len(matches) != 1:
                raise ValueError(f"{name} {product} 个人累计缺失或重复")
            shown = matches[0]
            if shown != "核实" and (not shown.isdecimal() or int(shown) != amount):
                raise ValueError(f"{name} {product} 个人累计 {shown} 与明细 {amount} 不符")
            if shown == "核实" and amount:
                raise ValueError(f"{name} {product} 有已报数却仍标核实")
        label = {"A": "A已报小计", "B": "B个人小计", "C": "C已报小计"}[code]
        matches = re.findall(r"^\| " + label + r" \| (\d+) \|", month.group(1), re.M)
        if len(matches) != 1 or int(matches[0]) != sum(sums.values()):
            raise ValueError(f"{name} 个人小计与明细不符")
        annual = text[text.index("## 2026年下半年年度汇总"):text.index("## 补录规则")]
        matches = re.findall(r"^\| " + code + "｜" + name + r" \| [^|]+ \| 已报小计 \| (\d+) \|", annual, re.M)
        if len(matches) != 1 or int(matches[0]) != sum(sums.values()):
            raise ValueError(f"{name} 年度已报小计与明细不符")


def check(before, after):
    previous, current = records(before), records(after)
    for key, value in previous.items():
        if key not in current:
            raise ValueError(f"旧记录被删除：{key}")
        if current[key] != value:
            raise ValueError(f"旧记录被改动：{key}")
    lost_messages = set(MESSAGE.findall(before)) - set(MESSAGE.findall(after))
    if lost_messages:
        raise ValueError("来源/确认消息丢失：" + ", ".join(sorted(lost_messages)))
    totals(after, current)
    print(f"校验通过：保留旧记录 {len(previous)} 条，现有 {len(current)} 条，个人累计吻合")


if __name__ == "__main__":
    try:
        if len(sys.argv) != 3:
            raise ValueError("用法：guard_production_ledger.py 旧版.md 候选版.md")
        check(Path(sys.argv[1]).read_text(encoding="utf-8"),
              Path(sys.argv[2]).read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        print(f"生产台账校验失败：{exc}", file=sys.stderr)
        sys.exit(1)
