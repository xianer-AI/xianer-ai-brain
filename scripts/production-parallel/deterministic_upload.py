"""Deterministic writer for the standard six-product production report.

The model is intentionally not involved in this path.  The source and
confirmation are still validated by upload_task/commit_guard; this module
only builds the complete markdown candidate from the latest remote snapshot.
Exit status 75 means that the report is outside the standard shape and the
caller may use the legacy review worker.
"""
import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import commit_guard
import upload_task
import coverage_tables
from service import DB

PRODUCTS = commit_guard.PRODUCTS
WORKERS = {
    'A': ('徐超超', '下机'),
    'B': ('梅芳', '下机'),
    'C': ('李鸿玉', '烤边'),
    'D': ('张小翠', '烤边'),
}
ENDPOINT = commit_guard.ENDPOINT
UNSUPPORTED = 75


def _remote(endpoint=None):
    value = commit_guard.gh_read_json(endpoint or ENDPOINT)
    return value['sha'], base64.b64decode(value['content']).decode('utf-8')


def _date_parts(value):
    match = re.fullmatch(r'(\d{4})-(\d{2})-(\d{2})', str(value or ''))
    if not match:
        raise ValueError('标准报数缺少有效生产日期')
    return match.group(0), int(match.group(1)), int(match.group(2)), int(match.group(3))


def _standard_report(task):
    inspected = upload_task.inspect(DB, task)
    extracted = (inspected['source_record'].get('result') or {}).get('extracted') or {}
    raw_items = extracted.get('items') or []
    raw_products = [item.get('product') for item in raw_items]
    if any(product not in PRODUCTS for product in raw_products):
        raise RuntimeError('存在非标准产品')
    if len(raw_products) != len(set(raw_products)):
        raise RuntimeError('产品重复，不能确定性入账')
    # A missing product was already shown on the current review card as
    # “0双（数量为0，请核实）”. The exact employee confirmation binds that
    # zero to this batch; keep the missing-product provenance separately.
    missing_products = set(PRODUCTS) - set(raw_products)
    report = commit_guard.report_from_inbox_row(inspected['source_record'])
    worker = report.get('worker')
    if worker not in WORKERS:
        raise RuntimeError('非标准员工代号')
    production_date, year, month, day = _date_parts(report.get('production_date'))
    if month < 1 or month > 12:
        raise RuntimeError('非标准生产日期')
    # Choices 1/2 are status confirmations.  They intentionally carry a
    # synthetic zero-item payload for queue compatibility, but must never be
    # forced through the six-product quantity validator.
    if report.get('status_only'):
        report.update({'production_date': production_date, 'year': year, 'month': month,
                       'values': {product: 0 for product in PRODUCTS},
                       'worker': worker, 'name': WORKERS[worker][0],
                       'process': WORKERS[worker][1], 'missing_products': set()})
        report['not_worked'] = report.get('attendance_status') == 'not_worked'
        report['already_reported'] = report.get('attendance_status') == 'already_reported'
        return inspected, report
    values = {}
    for item in report.get('items') or []:
        product = item.get('product')
        if product not in PRODUCTS:
            raise RuntimeError('存在非标准产品')
        try:
            quantity = int(item.get('quantity'))
        except (TypeError, ValueError):
            raise RuntimeError('数量不是整数')
        if quantity < 0:
            raise RuntimeError('数量不能为负数')
        values[product] = quantity
    if set(values) != set(PRODUCTS):
        raise RuntimeError('六项产品未完整解析')
    if report.get('total') != sum(values.values()):
        raise RuntimeError('合计与六项数量不一致')
    report.update({'production_date': production_date, 'year': year, 'month': month,
                   'values': values, 'worker': worker,
                   'name': WORKERS[worker][0], 'process': WORKERS[worker][1],
                   'missing_products': missing_products})
    report['not_worked'] = bool(report.get('not_worked'))
    report['already_reported'] = bool(report.get('already_reported'))
    return inspected, report


def _row_parts(line):
    return [part.strip() for part in line.strip().strip('|').split('|')]


def _replace_row(lines, predicate, fields):
    for index, line in enumerate(lines):
        if line.startswith('|') and predicate(_row_parts(line)):
            lines[index] = '| ' + ' | '.join(str(x) for x in fields) + ' |'
            return True
    return False


def _detail_bounds(text, worker):
    heading = f'## {worker}｜{WORKERS[worker][0]}{WORKERS[worker][1]}'
    start = text.find(heading)
    if start < 0:
        raise RuntimeError('远程台账没有对应员工明细区')
    end = text.find('\n## ', start + len(heading))
    return start, len(text) if end < 0 else end


def _next_ids(section, worker, date):
    pattern = re.compile(rf'^\| (20\d{{6}}-{worker}-\d{{3}}) \|', re.M)
    numbers = [int(match.group(1)[-3:]) for match in pattern.finditer(section)]
    begin = max(numbers, default=0) + 1
    return [f'{date.replace("-", "")}-{worker}-{begin + offset:03d}' for offset in range(6)]


def _has_valid_detail_record(section, worker, date):
    """Return whether a date subsection contains an effective detail row.

    The ledger keeps historical date headings even when a date has no valid
    production record (for example, a ``暂无有效记录`` placeholder).  A heading
    alone must not block a backfill.  Only a detail row with the date/worker
    record ID and an active confirmation status counts as an existing record.
    """
    heading = re.search(rf'^###\s+{re.escape(date)}｜[^\n]*\n', section, re.M)
    if not heading:
        return False
    body_match = re.search(r'^###\s+', section[heading.end():], re.M)
    body = section[heading.end():heading.end() + body_match.start()] if body_match else section[heading.end():]
    record = re.compile(
        rf'^\|\s*{date.replace("-", "")}-{re.escape(worker)}-\d{{3}}\s*\|.*\|\s*已确认[^|]*\|\s*$',
        re.M,
    )
    return bool(record.search(body))


def _insert_detail(text, worker, report, source, confirmation):
    start, end = _detail_bounds(text, worker)
    section = text[start:end]
    date = report['production_date']
    ids = _next_ids(section, worker, date)
    if source in section or confirmation in section:
        raise ValueError('来源或确认消息已经在远程明细中，禁止重复入账')
    # Historical backfills are explicitly date-scoped.  Refuse a second
    # upload for the same worker/date before constructing a candidate; an
    # existing valid row must be handled through the correction workflow.
    # Ordinary uploads retain the legacy behavior so an explicitly reviewed
    # same-day correction is not silently changed by this guard.
    if report.get('backfill') and _has_valid_detail_record(section, worker, date):
        raise ValueError(f'{worker}员工{date}已有有效记录，禁止重复补报')
    values = report['values']
    rows = '\n'.join(
        f'| {record_id} | {product} | {values[product]} | 已确认 |'
        for record_id, product in zip(ids, PRODUCTS)
    )
    missing = set(report.get('missing_products') or ())
    explicit_zero = [product for product in PRODUCTS if values[product] == 0 and product not in missing]
    notes = []
    if explicit_zero:
        notes.append('明确报0项：' + '、'.join(explicit_zero) + '。')
    if missing:
        notes.append('原始未报项：' + '、'.join(product for product in PRODUCTS if product in missing) +
                     '；经员工本人本次“准确”确认按0双写入。')
    date_note = (
        '生产日期按员工补报中明确填写的日期入账；'
        if report.get('backfill') else
        '生产日期按消息时间（上海时区07:00切日）自动归属；'
    )
    note = (f'当日合计：{sum(values.values())}双。{date_note}'
            f'班次未提供。' + ''.join(notes) if notes else
            f'当日合计：{sum(values.values())}双。{date_note}班次未提供。')
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', '+00:00')
    block = (f'### {date}｜{report["name"]}{report["process"]}\n\n'
             '| 记录编号 | 产品 | 数量（双） | 状态 |\n|---|---|---:|---|\n'
             f'{rows}\n\n{note}\n\n来源消息：{source}\n\n确认消息：{confirmation}\n\n'
             f'上报日期时间（UTC）：{now}\n\n')
    marker = section.find('\n### ')
    if marker < 0:
        marker = len(section)
    section = section[:marker + 1] + block + section[marker + 1:]
    return text[:start] + section + text[end:]


def _insert_attendance_status(text, worker, report, source, confirmation):
    """Append a confirmed attendance state without creating production rows."""
    status = report.get('attendance_status') or ('not_worked' if report.get('not_worked') else 'already_reported')
    label = '已确认未上班' if status == 'not_worked' else '已报待查'
    marker = '### 已确认出勤状态日期'
    legacy_marker = '### 已确认未上班日期'
    row = (f'| {worker}｜{report["name"]} | {report["production_date"]} | '
           f'{label}（不计入生产统计） | {source} | {confirmation} |')
    if source in text or confirmation in text:
        return text
    start = text.find('## 人员生产记录覆盖情况')
    if start < 0:
        return text
    section_end_match = re.search(r'^## ', text[start + len('## 人员生产记录覆盖情况'):], re.M)
    section_end = start + len('## 人员生产记录覆盖情况') + section_end_match.start() if section_end_match else len(text)
    section = text[start:section_end]
    active_marker = marker if marker in section else (legacy_marker if legacy_marker in section else None)
    if active_marker:
        marker_pos = section.find(active_marker)
        next_heading = re.search(r'^### ', section[marker_pos + len(active_marker):], re.M)
        end = marker_pos + len(active_marker) + next_heading.start() if next_heading else len(section)
        chunk = section[marker_pos:end]
        if report['production_date'] in chunk and f'{worker}｜{report["name"]}' in chunk:
            raise ValueError(f'{worker}员工{report["production_date"]}已有出勤状态核实记录')
        insert_at = len(chunk.rstrip())
        chunk = chunk.rstrip() + '\n' + row + '\n'
        section = section[:marker_pos] + chunk + section[end:]
    else:
        pending_pos = section.find('### 待核实日期')
        block = ('### 已确认出勤状态日期\n\n'
                 '| 员工 | 日期 | 当前状态 | 来源消息 | 确认消息 |\n'
                 '|---|---|---|---|---|\n' + row + '\n\n')
        insert_at = pending_pos if pending_pos >= 0 else len(section)
        section = section[:insert_at] + block + section[insert_at:]
    return text[:start] + section + text[section_end:]


def _attendance_status_map(text):
    """Read confirmed attendance dates and their statuses from coverage."""
    result = {code: {} for code in WORKERS}
    markers = ('### 已确认出勤状态日期', '### 已确认未上班日期')
    chunks = []
    for marker in markers:
        start = text.find(marker)
        if start < 0:
            continue
        end_match = re.search(r'^### |^## ', text[start + len(marker):], re.M)
        end = start + len(marker) + end_match.start() if end_match else len(text)
        chunks.append(text[start:end])
    chunk = '\n'.join(chunks)
    if not chunk:
        return result
    for code in WORKERS:
        name = re.escape(WORKERS[code][0])
        for match in re.finditer(rf'^\|\s*{code}｜{name}\s*\|\s*([^|]+)\|\s*([^|]+)\|', chunk, re.M):
            raw = match.group(1).strip()
            state_text = match.group(2)
            iso = re.search(r'(20\d{2}-\d{2}-\d{2})', raw)
            if iso:
                status = 'not_worked' if '未上班' in state_text else 'already_reported'
                result[code][iso.group(1)] = status
                continue
            cn = re.search(r'(20\d{2})年(\d{1,2})月(\d{1,2})日', raw)
            if cn:
                value = f'{int(cn.group(1)):04d}-{int(cn.group(2)):02d}-{int(cn.group(3)):02d}'
                result[code][value] = 'not_worked' if '未上班' in state_text else 'already_reported'
    return result


def _records(text, worker, period=None):
    start, end = _detail_bounds(text, worker)
    section = text[start:end]
    row_re = re.compile(r'^\| (20\d{6}-' + worker + r'-\d{3}) \| ([^|]+) \| (\d+) \| [^|]+ \|$', re.M)
    totals = {product: 0 for product in PRODUCTS}
    dates = set()
    for match in row_re.finditer(section):
        product = match.group(2).strip()
        if product not in totals:
            continue
        record_date = f'{match.group(1)[:4]}-{match.group(1)[4:6]}-{match.group(1)[6:8]}'
        if period and record_date[:7] != period:
            continue
        totals[product] += int(match.group(3))
        dates.add(record_date)
    if not dates:
        raise RuntimeError('新增后没有可计算的员工明细')
    return totals, dates


def _month_heading(year, month):
    return f'{year}年{int(month)}月月度汇总'


def _daily_heading(year, month):
    return f'### {year}年{int(month)}月每日汇总'


def _month_section_bounds(text, year, month):
    """Find a month section without relying on its historical numbering."""
    heading = _month_heading(year, month)
    match = re.search(rf'^## [^\n]*{re.escape(heading)}\s*$', text, re.M)
    if not match:
        return None
    end_match = re.search(r'^## ', text[match.end():], re.M)
    end = match.end() + end_match.start() if end_match else len(text)
    return match.start(), end


def _month_rows(worker, values=None, latest='—', dates=(), *, empty=False):
    if values is None:
        values = {product: '—' for product in PRODUCTS}
        empty = True
    subtotal = '—' if empty else sum(values[product] for product in PRODUCTS)
    # An empty monthly row is a real employee/month slot.  Keep its product
    # cells and subtotal as dashes, but make the production-day count explicit
    # so it cannot be mistaken for an omitted field.
    day_count = '0日' if empty else f'{len(dates)}日'
    return ('| ' + ' | '.join([f'{worker}｜{WORKERS[worker][0]}', WORKERS[worker][1],
                               *[values[p] for p in PRODUCTS], subtotal, day_count, latest]) + ' |')


def _ensure_month_section(text, year, month):
    """Ensure a complete month table exists, preserving old sections verbatim."""
    bounds = _month_section_bounds(text, year, month)
    if bounds:
        start, end = bounds
        section = text[start:end]
        # Existing ledgers may predate D's mapping; add only missing worker
        # rows.  Normalize legacy empty rows while leaving real quantities and
        # explicit zero reports untouched.
        lines = section.splitlines()
        separator = next((i for i, line in enumerate(lines) if line.startswith('|---')), None)
        if separator is not None:
            present = {_row_parts(line)[0] for line in lines[separator + 1:] if line.startswith('|')}
            inserts = []
            for worker in WORKERS:
                key = f'{worker}｜{WORKERS[worker][0]}'
                if key not in present:
                    inserts.append(_month_rows(worker))
            for index in range(separator + 1, len(lines)):
                fields = _row_parts(lines[index]) if lines[index].startswith('|') else []
                if len(fields) < 11 or not fields[0].startswith(tuple(f'{code}｜' for code in WORKERS)):
                    continue
                if all(fields[position] == '—' for position in range(2, 9)) and fields[9] == '—':
                    fields[9] = '0日'
                    lines[index] = '| ' + ' | '.join(fields) + ' |'
            if inserts:
                insert_at = len(lines)
                for i in range(separator + 1, len(lines)):
                    if lines[i].startswith('|'):
                        insert_at = i + 1
                lines[insert_at:insert_at] = inserts
                section = '\n'.join(lines).rstrip('\n') + '\n'
                text = text[:start] + section + text[end:]
        return text

    # Insert a future month before the rules section (or at EOF), with all
    # four worker rows.  This keeps the 2026 October–December skeleton and
    # permits a newly created 2027 ledger to use exactly the same shape.
    marker = re.search(r'^## (?:七、完整规则原文|完整规则原文)', text, re.M)
    insert_at = marker.start() if marker else len(text)
    prefix = '' if insert_at == 0 or text[:insert_at].endswith('\n') else '\n'
    rows = '\n'.join(_month_rows(worker) for worker in WORKERS)
    block = (f'{prefix}## {len(re.findall(r"^## ", text[:insert_at], re.M)) + 1}、{_month_heading(year, month)}\n\n'
             '> 尚无有效记录，保留空白结构，不带入其他月份累计。\n\n'
             '| 人员 | 工序 | 棉堆堆袜 | 冰冰袜 | 小腿袜 | 过膝袜 | 女船袜 | 男船袜 | 本月已报小计 | 有记录的生产日数 | 最新已记生产日 |\n'
             '|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|\n'
             f'{rows}\n\n')
    return text[:insert_at] + block + text[insert_at:]


def _status(product, value, date, missing_products=None):
    if value == 0:
        if product in (missing_products or set()):
            return f'已确认（含{date}核实为0）'
        return f'已确认（含{date}明确报0）'
    return f'已确认（含{date}）'


def _personal_marker(worker, latest):
    year, month = latest[:4], int(latest[5:7])
    return f'### {worker}｜{year}年{month}月个人累计'


def _personal_block(worker, totals, latest, missing_products=None):
    """Build a month-scoped personal subtotal block.

    Historical ledgers use a details wrapper for these blocks.  New blocks
    retain that wrapper so old guards and readers can continue to parse them.
    """
    label = WORKERS[worker][1]
    rows = []
    for product in PRODUCTS:
        rows.append(f'| {product} | {totals[product]} | '
                    f'{_status(product, totals[product], latest, missing_products)} |')
    rows.append(f'| {worker}已报小计 | {sum(totals.values())} | 六项均已收到数量反馈 |')
    marker = _personal_marker(worker, latest)
    return ('<details>\n'
            f'<summary>当前个人累计：{worker}｜{latest[:4]}年{int(latest[5:7])}月个人累计</summary>\n\n'
            f'{marker}\n\n'
            f'截至 {latest}，以下只统计{WORKERS[worker][0]}本人当前月份的{label}记录。\n\n'
            '| 产品 | ' + label + '数量（双） | 状态/说明 |\n|---|---:|---|\n' +
            '\n'.join(rows) + '\n\n</details>\n\n')


def _update_personal(text, worker, totals, latest, missing_products=None):
    start, end = _detail_bounds(text, worker)
    section = text[start:end]
    marker = _personal_marker(worker, latest)
    pos = section.find(marker)
    if pos < 0:
        # A ledger may have been initialized before the current month was
        # known.  Append a new month-scoped section inside this employee's
        # detail area instead of failing with a generic upload error.
        block = _personal_block(worker, totals, latest, missing_products)
        return text[:start] + section.rstrip() + '\n\n' + block + text[end:]
    stop_candidates = [value for value in (
        section.find('\n</details>', pos),
        section.find('\n### ', pos + len(marker)),
        section.find('\n## ', pos + len(marker)),
    ) if value >= 0]
    stop = min(stop_candidates) if stop_candidates else -1
    if stop < 0:
        raise RuntimeError('个人累计区格式不完整')
    chunk = section[pos:stop]
    chunk = re.sub(r'截至 \d{4}-\d{2}-\d{2}', f'截至 {latest}', chunk, count=1)
    label = WORKERS[worker][1]
    for product in PRODUCTS:
        pattern = re.compile(rf'^\| {re.escape(product)} \| [^|]+ \| [^|]+ \|$', re.M)
        replacement = f'| {product} | {totals[product]} | {_status(product, totals[product], latest, missing_products)} |'
        chunk, count = pattern.subn(replacement, chunk, count=1)
        if count != 1:
            raise RuntimeError(f'个人累计缺少产品行：{product}')
    subtotal = sum(totals.values())
    # Historical ledger versions used both ``B已报小计`` and ``B个人小计``
    # in this section.  Keep the existing label while replacing only the
    # value; changing the label would create unnecessary history churn.
    subtotal_pattern = re.compile(
        rf'^\| ({re.escape(worker)}(?:已报|个人)小计) \| [^|]+ \| [^|]+ \|$',
        re.M,
    )
    chunk, count = subtotal_pattern.subn(
        lambda match: f'| {match.group(1)} | {subtotal} | 六项均已收到数量反馈 |',
        chunk,
        count=1,
    )
    if count != 1:
        raise RuntimeError(f'个人累计缺少小计行：{worker}')
    return text[:start + pos] + chunk + text[start + stop:end] + text[end:]


def _update_group_table(text, worker, totals, latest, missing_products=None):
    heading = f'### {worker}组｜{WORKERS[worker][0]}{WORKERS[worker][1]}'
    start = text.find(heading)
    if start < 0:
        raise RuntimeError('远程台账缺少下半年累计区')
    end = text.find('\n### ', start + len(heading))
    if end < 0:
        end = text.find('\n## ', start + len(heading))
    end = len(text) if end < 0 else end
    chunk = text[start:end]
    for product in PRODUCTS:
        pattern = re.compile(rf'^\| {re.escape(product)} \| [^|]+ \| [^|]+ \|$', re.M)
        replacement = f'| {product} | {totals[product]} | {_status(product, totals[product], latest, missing_products)} |'
        chunk, count = pattern.subn(replacement, chunk, count=1)
        if count != 1:
            raise RuntimeError(f'累计表缺少产品行：{product}')
    chunk = re.sub(rf'^\| \*\*{worker}已报小计\*\* \| \*\*[^|]+\*\* \| [^|]+ \|$',
                   f'| **{worker}已报小计** | **{sum(totals.values())}** | 六项均已收到数量反馈 |', chunk, count=1, flags=re.M)
    return text[:start] + chunk + text[end:]


def _update_annual(text, worker, totals, missing_products=None, latest=None):
    year = latest[:4] if latest else ''
    candidates = [f'## {year}年下半年累计汇总',
                  f'## {year}年下半年年度汇总（当前已报）',
                  f'## {year}年度累计汇总（当前已报）',
                  f'## 一、{year}年度累计汇总',
                  f'## 一、{year}年全年累计汇总']
    heading = next((value for value in candidates if value in text), None)
    start = text.find(heading) if heading else -1
    if start < 0:
        # Generated ledgers may number the annual section after the
        # coverage tables (for example ``## 三、2027年度累计汇总``).
        # Keep routing independent of that display-only section number.
        match = re.search(
            rf'^## (?:[^、\s]+、)?{re.escape(year)}(?:年(?:下半年|全年)?|年度)累计汇总(?:（当前已报）)?$',
            text, re.M,
        )
        if match:
            start = match.start()
            heading = match.group(0)
    if start < 0:
        raise RuntimeError('远程台账缺少年度累计区')
    # Stop at the first next top-level section or the enclosing details
    # boundary, whichever comes first.  Hidden appendices may appear later
    # in the document and must not pull monthly tables into the annual block.
    end_candidates = [
        value for value in (
            text.find('\n## ', start + len(heading)),
            text.find('\n</details>', start + len(heading)),
        ) if value >= 0
    ]
    end = min(end_candidates) if end_candidates else len(text)
    lines = text[start:end].splitlines()
    target = f'{worker}｜{WORKERS[worker][0]}'
    # New annual templates use one group table per worker and do not repeat
    # a flattened annual table.  _update_group_table updates that layout;
    # there is nothing to do here when no flattened target row exists.
    has_flat_rows = any(
        line.startswith('|') and len(_row_parts(line)) >= 5 and _row_parts(line)[0] == target
        for line in lines
    )
    if not has_flat_rows:
        return text
    for product in PRODUCTS:
        found = False
        for index, line in enumerate(lines):
            fields = _row_parts(line) if line.startswith('|') else []
            if len(fields) < 5 or fields[2] != product:
                continue
            if fields[0] == target or (fields[0] == '' and any(lines[j].startswith(f'| {target} |') for j in range(max(0, index - 6), index))):
                fields[3] = totals[product]
                if latest and totals[product] == 0:
                    fields[4] = _status(product, totals[product], latest, missing_products)
                lines[index] = '| ' + ' | '.join(str(x) for x in fields) + ' |'
                found = True
                break
        if not found:
            raise RuntimeError(f'年度累计缺少产品行：{product}')
    for index, line in enumerate(lines):
        fields = _row_parts(line) if line.startswith('|') else []
        if len(fields) >= 5 and fields[0] == target and fields[2] == '已报小计':
            fields[3] = sum(totals.values())
            lines[index] = '| ' + ' | '.join(str(x) for x in fields) + ' |'
            break
    else:
        raise RuntimeError('年度累计缺少小计行')
    return text[:start] + '\n'.join(lines).rstrip('\n') + '\n' + text[end:]


def _update_monthly(text, worker, totals, dates, latest):
    year, month = latest[:4], int(latest[5:7])
    bounds = _month_section_bounds(text, year, month)
    if not bounds:
        return text
    start, end = bounds
    chunk = text[start:end]
    target = f'{worker}｜{WORKERS[worker][0]}'
    fields = [target, WORKERS[worker][1], *[totals[p] for p in PRODUCTS], sum(totals.values()), f'{len(dates)}日', latest]
    lines = chunk.splitlines()
    if not _replace_row(lines, lambda f: len(f) >= 2 and f[0] == target, fields):
        insert_at = next((i + 1 for i, line in enumerate(lines) if line.startswith('|---')), None)
        if insert_at is None:
            return text
        empty = [target, WORKERS[worker][1], *['—' for _ in PRODUCTS], '—', '0日', '—']
        lines.insert(insert_at, '| ' + ' | '.join(empty) + ' |')
    return text[:start] + '\n'.join(lines).rstrip('\n') + '\n' + text[end:]


def _pending_queue_from_coverage(text):
    """Recover queue states from an existing coverage table.

    Production uploads may rebuild the coverage view before the durable queue
    snapshot is passed in.  Reading the old table here keeps an already-sent
    card visible during that short interval instead of downgrading it to a
    generic missing record.
    """
    result = []
    start = text.find('### 待核实日期')
    if start < 0:
        return result
    end_match = re.search(r'^### |^## ', text[start + len('### 待核实日期'):], re.M)
    end = start + len('### 待核实日期') + end_match.start() if end_match else len(text)
    chunk = text[start:end]
    states = {
        '核实卡已发送，等待回复': 'sent',
        '待核实，排队等待处理': 'pending',
        '核实卡发送失败，等待重试': 'failed',
    }
    for match in re.finditer(
        r'^\|\s*([ABCD])｜([^|]+)\s*\|\s*(20\d{2}年\d{1,2}月\d{1,2}日|20\d{2}-\d{2}-\d{2})\s*\|\s*([^|]+)\|',
        chunk, re.M,
    ):
        code, raw_date, state_text = match.group(1), match.group(3), match.group(4).strip()
        state = next((value for label, value in states.items() if label in state_text), None)
        if not state:
            continue
        iso = re.search(r'(20\d{2})年(\d{1,2})月(\d{1,2})日', raw_date)
        if iso:
            raw_date = f'{int(iso.group(1)):04d}-{int(iso.group(2)):02d}-{int(iso.group(3)):02d}'
        result.append({'worker': code, 'production_date': raw_date, 'state': state})
    return result


def _month_status_summary(text, year, month, pending_queue):
    """Build short status notes for one month directly below its table."""
    status_map = _attendance_status_map(text)
    pending = {}
    for item in pending_queue or ():
        code = str(item.get('worker') or '')
        raw_date = str(item.get('production_date') or '')
        if code not in WORKERS or not re.fullmatch(r'20\d{2}-\d{2}-\d{2}', raw_date):
            continue
        if raw_date[:7] != f'{year:04d}-{month:02d}':
            continue
        state = str(item.get('state') or 'pending')
        if state in {'pending', 'sent', 'failed'}:
            pending[(code, raw_date)] = state
    state_labels = {
        'sent': '待核实（核实卡已发送，等待回复）',
        'pending': '待核实（排队等待处理）',
        'failed': '待核实（核实卡发送失败，等待重试）',
    }
    entries = []
    for code, (name, _process) in WORKERS.items():
        pieces = []
        dates = set()
        for raw_date, status in sorted(status_map.get(code, {}).items()):
            if raw_date[:7] != f'{year:04d}-{month:02d}':
                continue
            dates.add(raw_date)
            day = int(raw_date[8:10])
            if status == 'not_worked':
                pieces.append(f'{month}月{day}日已确认未上班')
            else:
                pieces.append(f'{month}月{day}日已确认已报待查')
        for (pending_code, raw_date), state in sorted(pending.items()):
            if pending_code != code or raw_date in dates:
                continue
            day = int(raw_date[8:10])
            pieces.append(f'{month}月{day}日{state_labels[state]}')
        if pieces:
            entries.append(f'> {code}｜{name}：' + '；'.join(pieces) + '。')
    return entries


def _update_month_status_notes(text, pending_queue):
    """Place a de-duplicated status note immediately below each month table."""
    month_matches = list(re.finditer(
        r'^## [^\n]*?(20\d{2})年(\d{1,2})月月度汇总\s*$', text, re.M
    ))
    for match in reversed(month_matches):
        year, month = int(match.group(1)), int(match.group(2))
        bounds = _month_section_bounds(text, year, month)
        if not bounds:
            continue
        start, end = bounds
        section = text[start:end]
        marker = f'<!-- monthly-status-summary:{year:04d}-{month:02d} -->'
        close_marker = f'<!-- /monthly-status-summary:{year:04d}-{month:02d} -->'
        old = re.compile(
            rf'\n?{re.escape(marker)}\n.*?{re.escape(close_marker)}\n?',
            re.S,
        )
        section = old.sub('\n', section)
        notes = _month_status_summary(text, year, month, pending_queue)
        if notes:
            block = marker + '\n' + '\n'.join(notes) + '\n' + close_marker
            daily = section.find(_daily_heading(year, month))
            insert_at = daily if daily >= 0 else len(section)
            before = section[:insert_at].rstrip()
            after = section[insert_at:].lstrip('\n')
            section = before + '\n\n' + block + ('\n\n' + after if after else '\n')
        text = text[:start] + section + text[end:]
    return text


def _normalize_monthly_empty_rows(text):
    """Normalize every legacy empty monthly row to an explicit ``0日``."""
    month_matches = list(re.finditer(
        r'^## [^\n]*?(20\d{2})年(\d{1,2})月月度汇总\s*$', text, re.M
    ))
    for match in reversed(month_matches):
        bounds = _month_section_bounds(text, int(match.group(1)), int(match.group(2)))
        if not bounds:
            continue
        start, end = bounds
        lines = text[start:end].splitlines()
        separator = next((i for i, line in enumerate(lines) if line.startswith('|---')), None)
        if separator is None:
            continue
        changed = False
        for index in range(separator + 1, len(lines)):
            fields = _row_parts(lines[index]) if lines[index].startswith('|') else []
            if len(fields) < 11 or not any(fields[0] == f'{code}｜{name}' for code, (name, _process) in WORKERS.items()):
                continue
            if all(fields[position] == '—' for position in range(2, 9)) and fields[9] == '—':
                fields[9] = '0日'
                lines[index] = '| ' + ' | '.join(fields) + ' |'
                changed = True
        if changed:
            text = text[:start] + '\n'.join(lines).rstrip('\n') + '\n' + text[end:]
    return text


_VERSION_SECTION_RE = re.compile(
    r'^## (?:十二、)?三端当前版本与同步状态（自动维护）\s*$',
    re.M,
    # noqa: E501
)


def _version_status_block():
    """Read the canonical version/sync block from VERSION.md."""
    version_path = Path(__file__).resolve().parents[2] / '袜子生产制造袜子厂' / '生产统计工作台' / 'VERSION.md'
    try:
        source = version_path.read_text(encoding='utf-8')
        match = re.search(r'^## 三端当前版本与同步状态（自动维护）\s*$.*?(?=^## |\Z)', source, re.M | re.S)
        if match:
            return match.group(0).rstrip() + '\n'
    except OSError:
        pass
    return ('## 三端当前版本与同步状态（自动维护）\n\n'
            '本区域不属于年度台账，也不记录员工生产数据；它只用于查看 GitHub、OpenClaw 和飞书当前是否使用同一套规则。\n\n'
            '| 端 | 当前版本 | 对应提交/标识 | 最后同步时间 | 状态 |\n'
            '|---|---|---|---|---|\n'
            '| GitHub | 由规则源自动读取 | VERSION.json.github_commit | 自动记录 | 自动判断 |\n'
            '| OpenClaw | 由实际加载的 VERSION.json 自动读取 | sync_protocol_version / card_protocol_version | 自动记录 | 自动判断 |\n'
            '| 飞书 | 由当前卡片模板和回读结果自动读取 | 卡片协议版本 / 平台回读标识 | 自动记录 | 自动判断 |\n')


def _update_version_status_section(text, year):
    """Mirror VERSION.md's canonical sync-status block into a yearly ledger."""
    anchor = f'<a id="version-sync-{year}"></a>\n\n'
    block = anchor + _version_status_block()
    start_match = _VERSION_SECTION_RE.search(text)
    return_link = f'[↑ 返回目录](#ledger-toc-{year})'
    if start_match:
        start = start_match.start()
        prior_anchor = text.rfind(anchor.rstrip('\n'), 0, start)
        if prior_anchor >= 0 and not text[prior_anchor + len(anchor.rstrip('\n')):start].strip():
            start = prior_anchor
        return_pos = text.find(return_link, start_match.end())
        # The block lives at the end of the audit appendix.  Replacing all
        # content through the final directory link also collapses any legacy
        # duplicate blocks created by an older non-idempotent runtime.
        end = return_pos if return_pos >= 0 else len(text)
        return text[:start] + block + '\n' + text[end:]
    insert_at = text.rfind(return_link)
    if insert_at < 0:
        insert_at = len(text)
    prefix = '' if insert_at == 0 or text[:insert_at].endswith('\n') else '\n'
    return text[:insert_at] + prefix + block + '\n' + text[insert_at:]


def _ensure_version_toc_link(text, year):
    anchor = f'#version-sync-{year}'
    if anchor in text:
        return text
    link = f'[三端当前版本与同步状态（自动维护）]({anchor})'
    if year == 2026:
        needle = re.compile(r'^11\. \[十一、更新记录\].*$', re.M)
    else:
        needle = re.compile(r'^18\. \[附录：审计与版本记录\].*$', re.M)
    match = needle.search(text)
    if not match:
        return text
    if year == 2026:
        line = f'12. {link}'
    else:
        line = f'   - {link}'
    return text[:match.end()] + '\n' + line + text[match.end():]


def _update_coverage(text, worker, dates, latest, *, pending_queue=None):
    """Replace the derived coverage section with the compact two-table view.

    The old implementation appended every date to one wide cell and treated
    any absent date as a missing-record list.  This renderer keeps the source
    detail rows untouched and derives a fixed four-row overview plus one row
    per month.  Empty cells are informational only; they are not upload or
    attendance decisions.
    """
    marker = '## 人员生产记录覆盖情况'
    start = text.find(marker)
    if start < 0:
        return text
    # The historical ledger keeps the directory between the coverage block
    # and the old numbered overview. Replace both old overview sections while
    # preserving the directory links and the first real monthly section.
    tail = text[start + len(marker):]
    toc_match = re.search(r'^## 目录\s*$', tail, re.M)
    third_match = re.search(r'^## 三、', tail, re.M)
    if toc_match and third_match and toc_match.start() < third_match.start():
        toc_start = start + len(marker) + toc_match.start()
        old_overview = re.search(
            r'^## 一、人员总览\s*$',
            tail[toc_match.end():third_match.start()],
            re.M,
        )
        if old_overview:
            old_overview_start = start + len(marker) + toc_match.end() + old_overview.start()
            directory = text[toc_start:old_overview_start]
            end = start + len(marker) + third_match.start()
            keep_directory = True
        else:
            directory = ''
            keep_directory = False
    else:
        directory = ''
        keep_directory = False
    if not keep_directory:
        end_match = re.search(r'^## (?!人员生产记录覆盖情况)', tail, re.M)
        end = start + len(marker) + end_match.start() if end_match else len(text)
    # Preserve operational queue states if this invocation came from a
    # production upload rather than the scheduler's durable snapshot.
    effective_pending = list(pending_queue) if pending_queue is not None else _pending_queue_from_coverage(text)
    date_map = {}
    backfill_map = {}
    for code in WORKERS:
        try:
            _, worker_dates = _records(text, code)
        except RuntimeError:
            worker_dates = set()
        date_map[code] = worker_dates
        start_code, end_code = _detail_bounds(text, code)
        section = text[start_code:end_code]
        backfill_map[code] = {
            match.group(1)
            for match in re.finditer(
                r'^### (20\d{2}-\d{2}-\d{2})｜.*?(?=^### |\Z)',
                section, re.M | re.S,
            )
            if '生产日期按员工补报中明确填写的日期入账；' in match.group(0)
        }
    month_matches = re.finditer(
        r'^## [^\n]*?(20\d{2})年(\d{1,2})月月度汇总\s*$', text, re.M
    )
    months = [(int(match.group(1)), int(match.group(2))) for match in month_matches]
    rendered = coverage_tables.render(
        date_map, months, backfill_map=backfill_map,
        attendance_status_map=_attendance_status_map(text),
        pending_queue=effective_pending,
    )
    candidate = text[:start] + rendered + (directory if keep_directory else '') + text[end:]
    # Keep the explicit return-directory anchor in the generated ledger.  The
    # coverage replacement starts before the directory and would otherwise
    # drop an anchor that sits immediately above ``## 目录`` on every status
    # refresh.  Reinsert one canonical anchor so the scheduler cannot undo
    # the working return links.
    year = months[0][0] if months else int(latest[:4]) if latest and re.match(r'20\d{2}-', latest) else None
    if year:
        candidate = re.sub(r'^<a id="ledger-toc-20\d{2}"></a>\s*\n?', '', candidate, flags=re.M)
        toc_heading = candidate.find('## 目录')
        if toc_heading >= 0:
            candidate = (
                candidate[:toc_heading]
                + f'<a id="ledger-toc-{year}"></a>\n\n'
                + candidate[toc_heading:]
            )
    candidate = _update_month_status_notes(candidate, effective_pending)
    candidate = _normalize_monthly_empty_rows(candidate)
    if year:
        candidate = _ensure_version_toc_link(candidate, year)
        candidate = _update_version_status_section(candidate, year)
    return candidate


def _update_daily(text, worker, report):
    date = report['production_date']
    heading = f'#### {date}'
    fields = [f'{report["worker"]}｜{report["name"]}', report['process'], *[report['values'][p] for p in PRODUCTS], sum(report['values'].values())]
    year, month = date[:4], int(date[5:7])
    bounds = _month_section_bounds(text, year, month)
    if not bounds:
        return text
    month_start, month_end = bounds
    month_text = text[month_start:month_end]
    daily_marker = _daily_heading(year, month)
    if heading not in month_text:
        pos = month_text.find(daily_marker)
        if pos < 0:
            # Add a daily section at the end of the month table.  The month
            # section itself remains date-scoped, so October cannot pollute
            # September or the next year's ledger.
            insert_at = len(month_text)
            block = (f'\n{daily_marker}\n\n#### {heading[5:]}\n\n| 人员 | 工序 | 棉堆堆袜 | 冰冰袜 | 小腿袜 | 过膝袜 | 女船袜 | 男船袜 | 当日已报小计 |\n'
                     '|---|---|---:|---:|---:|---:|---:|---:|---:|\n| ' + ' | '.join(str(x) for x in fields) + ' |\n\n')
            return text[:month_start] + month_text + block + text[month_end:]
        insert_at = month_text.find('\n', pos) + 1
        block = (f'\n{heading}\n\n| 人员 | 工序 | 棉堆堆袜 | 冰冰袜 | 小腿袜 | 过膝袜 | 女船袜 | 男船袜 | 当日已报小计 |\n'
                 '|---|---|---:|---:|---:|---:|---:|---:|---:|\n| ' + ' | '.join(str(x) for x in fields) + ' |\n\n')
        return text[:month_start] + month_text[:insert_at] + block + month_text[insert_at:] + text[month_end:]
    start = month_start + month_text.find(heading)
    end = text.find('\n#### ', start + len(heading), month_end)
    end = month_end if end < 0 else end
    lines = text[start:end].splitlines()
    target = fields[0]
    if not _replace_row(lines, lambda f: len(f) >= 2 and f[0] == target, fields):
        insert_at = None
        for i, line in enumerate(lines):
            row = _row_parts(line) if line.startswith('|') else []
            if len(row) >= 2 and '｜' in row[0] and row[0][0] in 'ABCD':
                insert_at = i + 1
        if insert_at is not None:
            lines.insert(insert_at, '| ' + ' | '.join(str(x) for x in fields) + ' |')
        else:
            insert_at = None
        for i, line in enumerate(lines):
            if insert_at is None and line.startswith('|---'):
                lines.insert(i + 1, '| ' + ' | '.join(str(x) for x in fields) + ' |')
                break
    return text[:start] + '\n'.join(lines) + text[end:]


def _update_log(text, report, source, confirmation):
    marker = '## 更新记录'
    start = text.find(marker)
    if start < 0:
        return text
    header = text.find('| 日期 | 更新内容 | 结果 |', start)
    modern_header = False
    if header < 0:
        header = text.find('| 日期 | 更新内容 | 影响范围 | GitHub提交 |', start)
        modern_header = header >= 0
    if header < 0:
        return text
    separator = text.find('\n', header) + 1
    separator = text.find('\n', separator) + 1
    values = report['values']
    if report.get('status_only') or report.get('not_worked') or report.get('already_reported'):
        status_label = '当天未上班' if report.get('not_worked') else '已经报过'
        if modern_header:
            row = (f'| {report["production_date"]} | 确认状态 {report["worker"]} | '
                   f'{report["name"]}（{report["worker"]}）{report["process"]}：{status_label}，不计入生产统计；'
                   f'来源消息：{source}；确认消息：{confirmation}；保留出勤核实状态，未写入生产明细或累计 | 待同步 |\n')
        else:
            row = (f'| {report["production_date"]} | 确认状态 {report["worker"]} | '
                   f'{report["name"]}（{report["worker"]}）{report["process"]}：{status_label}，不计入生产统计；'
                   f'来源消息：{source}；确认消息：{confirmation}；保留出勤核实状态，未写入生产明细或累计 |\n')
        return text[:separator] + row + text[separator:]
    detail = '、'.join(f'{p}{values[p]}' for p in PRODUCTS)
    missing = set(report.get('missing_products') or ())
    missing_note = ('；原始未报项：' + '、'.join(p for p in PRODUCTS if p in missing) +
                    '，经本人“准确”确认按0双入账') if missing else ''
    year_month = f'{report["year"]}年{report["month"]}月'
    action = '补报并上传' if report.get('backfill') else '上传'
    row = (f'| {report["production_date"]} | {action} {report["worker"]} 当日已确认报数 | '
           f'{report["name"]}（{report["worker"]}）{report["process"]}：{detail}双，合计{sum(values.values())}双{missing_note}；'
           f'来源消息：{source}；确认消息：{confirmation}；同步更新{year_month}月度、当日日报及对应年度累计 |\n')
    return text[:separator] + row + text[separator:]


def build_candidate(before, report, source, confirmation):
    worker = report['worker']
    missing_products = set(report.get('missing_products') or ())
    latest = report['production_date']
    if 'year' not in report or 'month' not in report:
        _, report_year, report_month, _ = _date_parts(latest)
        report = dict(report, year=report_year, month=report_month)
    period = latest[:7]
    if report.get('status_only') or report.get('not_worked') or report.get('already_reported'):
        after = _insert_attendance_status(before, worker, report, source, confirmation)
        try:
            all_totals, all_dates = _records(after, worker)
        except RuntimeError:
            all_totals, all_dates = ({product: 0 for product in PRODUCTS}, set())
        month_totals, month_dates = _records(after, worker, period) if all_dates else ({product: 0 for product in PRODUCTS}, set())
        month_latest = max(month_dates) if month_dates else None
        after = _ensure_month_section(after, report['year'], report['month'])
        if all_dates:
            month_latest = max(month_dates) if month_dates else None
            all_latest = max(all_dates)
            if month_latest:
                after = _update_personal(after, worker, month_totals, month_latest, missing_products)
            after = _update_annual(after, worker, all_totals, missing_products, all_latest)
            after = _update_group_table(after, worker, all_totals, all_latest, missing_products)
        # Preserve a row for a status-only date in the month summary.  The
        # row contains dashes/0 days and does not contribute to production.
        after = _update_monthly(after, worker, month_totals, month_dates, month_latest or latest)
        after = _update_coverage(after, worker, all_dates, max(all_dates) if all_dates else latest)
        after = _update_log(after, report, source, confirmation)
        if before.endswith('\n') and not after.endswith('\n'):
            after += '\n'
        return after
    after = _insert_detail(before, worker, report, source, confirmation)
    all_totals, all_dates = _records(after, worker)
    month_totals, month_dates = _records(after, worker, period)
    month_latest = max(month_dates)
    all_latest = max(all_dates)
    # Month and personal projections must never absorb prior months.  The
    # annual/half-year projections intentionally use all valid detail rows.
    after = _ensure_month_section(after, report['year'], report['month'])
    after = _update_personal(after, worker, month_totals, month_latest, missing_products)
    after = _update_annual(after, worker, all_totals, missing_products, all_latest)
    after = _update_group_table(after, worker, all_totals, all_latest, missing_products)
    after = _update_monthly(after, worker, month_totals, month_dates, month_latest)
    after = _update_coverage(after, worker, all_dates, all_latest)
    after = _update_daily(after, worker, report)
    after = _update_log(after, report, source, confirmation)
    if before.endswith('\n') and not after.endswith('\n'):
        after += '\n'
    return after


def run(task_path):
    inspected, report = _standard_report(task_path)
    source, confirmation = inspected['source'], inspected['confirmation']
    endpoint = commit_guard.endpoint_for_date(report['production_date'])
    receipt_path = Path(DB).parent / 'receipts' / (hashlib.sha256(source.encode()).hexdigest() + '.json')
    if receipt_path.exists():
        try:
            receipt = json.loads(receipt_path.read_text())
            if receipt.get('status') == 'verified' and receipt.get('source') == source and receipt.get('confirmation') == confirmation:
                print(json.dumps(receipt, ensure_ascii=False))
                return receipt
        except (OSError, ValueError):
            pass
    expected_sha, before = _remote(endpoint)
    if source in before or confirmation in before:
        raise ValueError('来源或确认消息已在远程台账中，但本地没有已验证回执，停止自动重复写入')
    candidate = build_candidate(before, report, source, confirmation)
    commit_guard.run_original_ledger_guard(before, candidate)
    with tempfile.NamedTemporaryFile('w', encoding='utf-8', suffix='.md', delete=False) as handle:
        handle.write(candidate)
        candidate_path = handle.name
    try:
        missing = set(report.get('missing_products') or ())
        missing_note = ('原始未报项 ' + '、'.join(p for p in PRODUCTS if p in missing) +
                        ' 经本次准确确认按0双入账；') if missing else ''
        note = (f'任务内原报数与员工本人准确确认由 inspect 核验并绑定同一核对卡；'
                f'{report["worker"]}={report["name"]}，{report["production_date"]} {report["process"]}，'
                f'六项按确认结果写入；{missing_note}明确报0项目按本次准确确认写入0双；候选从远程最新全文生成，'
                f'保留旧记录，并经原有 guard 校验通过。')
        environment = dict(os.environ)
        environment['PRODUCTION_LEDGER_ENDPOINT'] = endpoint
        proc = subprocess.run([sys.executable, str(Path(upload_task.__file__).resolve()), 'commit',
                               '--task', str(inspected['task']), '--expected-sha', expected_sha,
                               '--file', candidate_path, '--review-note', note],
                              text=True, capture_output=True, env=environment)
        if proc.returncode:
            raise RuntimeError((proc.stderr or proc.stdout or '确定性上传失败').strip()[-1200:])
        receipt = json.loads((proc.stdout or '').strip().splitlines()[-1])
        print(json.dumps(receipt, ensure_ascii=False))
        return receipt
    finally:
        Path(candidate_path).unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--task', required=True)
    parser.add_argument('--lease-key')
    args = parser.parse_args()
    try:
        run(args.task)
    except RuntimeError as exc:
        if str(exc) in {'非标准员工代号', '非标准生产日期', '存在非标准产品', '数量不是整数', '数量不能为负数', '六项产品未完整解析', '合计与六项数量不一致'}:
            print('确定性路径不适用：' + str(exc), file=sys.stderr)
            raise SystemExit(UNSUPPORTED)
        print('确定性上传失败：' + str(exc), file=sys.stderr)
        raise SystemExit(1)
    except (ValueError, OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        print('确定性上传失败：' + str(exc), file=sys.stderr)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
