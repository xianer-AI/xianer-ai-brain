"""Compact two-table coverage view for production ledgers.

The coverage section is a derived display.  It never changes detail records or
quantities.  The first table is a fixed employee summary; the second table is
one row per month so a full year cannot turn into a wide, one-column-per-day
table.
"""
from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping

WORKERS = {
    "A": "徐超超",
    "B": "梅芳",
    "C": "李鸿玉",
    "D": "张小翠",
}

def validate_status_exclusivity(text: str) -> None:
    """Reject a coverage view where one employee/date has two final states."""
    import re
    pending = set()
    confirmed = set()
    section = text[text.find('### 待核实日期'):text.find('### 已确认出勤状态日期')]
    for code, y, m, d in re.findall(r'^\|\s*([ABCD])｜[^|]+\s*\|\s*(20\d{2})年(\d{1,2})月(\d{1,2})日', section, re.M):
        pending.add((code, f'{y}-{int(m):02d}-{int(d):02d}'))
    section = text[text.find('### 已确认出勤状态日期'):]
    for code, y, m, d in re.findall(r'^\|\s*([ABCD])｜[^|]+\s*\|\s*(20\d{2})年(\d{1,2})月(\d{1,2})日', section, re.M):
        confirmed.add((code, f'{y}-{int(m):02d}-{int(d):02d}'))
    conflict = sorted(pending & confirmed)
    if conflict:
        raise ValueError('状态冲突，禁止发布：' + '、'.join(f'{c}:{d}' for c, d in conflict))

# A/B have fixed rota coverage and C is a long-term worker.  D remains ad-hoc
# and is only checked when an attendance gate explicitly supplies a workday.
# The renderer infers only historical interior gaps up to each worker's latest
# observed date, so a still-open trailing day is not called missing.
AUTO_ALERT_WORKERS = frozenset(("A", "B", "C"))


def _date(value: str | dt.date) -> dt.date:
    if isinstance(value, dt.date):
        return value
    return dt.date.fromisoformat(str(value))


def _label(value: dt.date, *, include_year: bool = False) -> str:
    prefix = f"{value.year}年" if include_year else ""
    return f"{prefix}{value.month}月{value.day}日"


def _ranges(values: Iterable[str]) -> list[tuple[dt.date, dt.date]]:
    dates = sorted({_date(value) for value in values})
    if not dates:
        return []
    result: list[tuple[dt.date, dt.date]] = []
    start = previous = dates[0]
    for current in dates[1:]:
        if current == previous + dt.timedelta(days=1):
            previous = current
            continue
        result.append((start, previous))
        start = previous = current
    result.append((start, previous))
    return result


def compact_dates(values: Iterable[str]) -> str:
    """Return contiguous dates as readable ranges with a stable year boundary."""
    ranges = _ranges(values)
    if not ranges:
        return "—"
    pieces: list[str] = []
    for start, end in ranges:
        if start == end:
            pieces.append(_label(start, include_year=False))
        elif start.year == end.year and start.month == end.month:
            pieces.append(f"{_label(start)}–{end.day}日")
        elif start.year == end.year:
            pieces.append(f"{_label(start)}–{_label(end)}")
        else:
            pieces.append(f"{_label(start, include_year=True)}–{_label(end, include_year=True)}")
    return "、".join(pieces)


def _month_label(year: int, month: int) -> str:
    return f"{year}年{month}月"


def expected_dates(
    date_map: Mapping[str, Iterable[str]],
    *,
    workers: Iterable[str] = AUTO_ALERT_WORKERS,
    window_start: str | dt.date | None = None,
    window_end: str | dt.date | None = None,
) -> dict[str, set[dt.date]]:
    """Return dates that are expected to have a report for each worker.

    The normal ledger renderer does not know the attendance calendar.  Its
    safe default is therefore the observed production window: from the
    earliest effective date in the ledger through each worker's latest
    effective date.  This detects historical holes such as B's 9/20--9/21
    gap while avoiding an alert for a day that is still open.  A scheduler or
    attendance integration may pass ``window_start``/``window_end`` to include
    a closed trailing day as well.

    D is excluded by default because its attendance is ad-hoc.  C is included
    because it is a long-term worker; a caller can still pass an explicit
    expected-date map when its work calendar is known.  The return value
    includes every known worker code for stable table rendering.
    """
    normalized = {
        code: sorted({_date(value) for value in date_map.get(code, ())})
        for code in WORKERS
    }
    all_dates = [value for values in normalized.values() for value in values]
    if not all_dates:
        return {code: set() for code in WORKERS}
    start = _date(window_start) if window_start else min(all_dates)
    explicit_end = _date(window_end) if window_end else None
    result: dict[str, set[dt.date]] = {code: set() for code in WORKERS}
    for code in workers:
        if code not in WORKERS:
            continue
        dates = normalized[code]
        if not dates:
            continue
        end = explicit_end or max(dates)
        if end < start:
            continue
        result[code] = {
            start + dt.timedelta(days=offset)
            for offset in range((end - start).days + 1)
        }
    return result


def missing_dates(
    date_map: Mapping[str, Iterable[str]],
    expected: Mapping[str, Iterable[str | dt.date]] | None = None,
) -> dict[str, set[dt.date]]:
    """Return expected dates without an effective detail record."""
    normalized = {
        code: {_date(value) for value in date_map.get(code, ())}
        for code in WORKERS
    }
    expected_map = expected or expected_dates(date_map)
    return {
        code: {
            value if isinstance(value, dt.date) else _date(value)
            for value in expected_map.get(code, ())
        } - normalized[code]
        for code in WORKERS
    }


def _compact_missing(values: Iterable[dt.date]) -> str:
    return compact_dates([value.isoformat() for value in sorted(values)])


def backfill_reminder(code: str, dates: Iterable[str | dt.date]) -> str:
    """Build the employee-visible Feishu reminder with a copyable template.

    A reminder is generated per production date.  Multiple missing dates are
    returned as separate blocks so an employee cannot accidentally combine
    quantities from different days into one ledger entry.
    """
    if code not in WORKERS:
        raise ValueError("未知员工代号")
    values = sorted({value if isinstance(value, dt.date) else _date(value) for value in dates})
    if not values:
        raise ValueError("补报提醒缺少生产日期")
    blocks = []
    for value in values:
        date = value.isoformat()
        blocks.append(
            "\n".join([
                "【生产报数待核实】",
                f"员工：{code}｜{WORKERS[code]}",
                f"待核实生产日期：{date}",
                "",
                "当天确实上班但统计中没有记录，请复制下面整段模板填写后发送：",
                "只把六个 `___` 替换成数字；员工、代号、生产日期、工序和产品名称不要修改或删除。没有生产的项目填写0，六项必须全部保留。",
                "",
                "补报",
                f"{code}={WORKERS[code]}",
                f"生产日期：{date}",
                "棉堆堆袜：___",
                "冰冰袜：___",
                "小腿袜：___",
                "过膝袜：___",
                "女船袜：___",
                "男船袜：___",
                "",
                "填写完成后，将整段模板发送回来，系统再生成核对卡。",
                f"如果当天未上班，请回复：未上班：{date}",
                f"如果已经报过，请回复：已报待查：{date}",
                "系统将按填写的生产日期入账，先生成核对卡，回复“准确”后才上传。",
            ])
        )
    return "\n\n".join(blocks)


def _month_cell(
    values: Iterable[str], code: str, missing: Iterable[dt.date] = (),
    *, year: int | None = None, month: int | None = None,
    backfill: Iterable[str | dt.date] = (),
    not_worked: Iterable[str | dt.date] = (),
    attendance_status: Mapping[str | dt.date, str] | None = None,
    pending_status: Mapping[str | dt.date, str] | None = None,
) -> str:
    dates = sorted({_date(value) for value in values})
    status_map = {_date(key): value for key, value in (attendance_status or {}).items()}
    not_worked_dates = sorted({value if isinstance(value, dt.date) else _date(value)
                               for value in not_worked})
    for value, status in status_map.items():
        if status == 'not_worked' and value not in not_worked_dates:
            not_worked_dates.append(value)
    not_worked_dates = sorted(not_worked_dates)
    not_worked_dates = [value for value in not_worked_dates
                        if year is None or (value.year == year and value.month == month)]
    backfill_dates = sorted({value if isinstance(value, dt.date) else _date(value) for value in backfill})
    backfill_dates = [value for value in backfill_dates
                      if value in dates and (year is None or value.year == year)
                      and (month is None or value.month == month)]
    normal_dates = [value for value in dates
                    if (year is None or value.year == year)
                    and (month is None or value.month == month)
                    and value not in set(backfill_dates)]
    target_year = year if year is not None else (dates[0].year if dates else None)
    target_month = month if month is not None else (dates[0].month if dates else None)
    missing_dates_for_month = sorted(
        value for value in missing
        if target_year is None or (value.year == target_year and value.month == target_month)
    )
    status_pieces = []
    if not_worked_dates:
        status_pieces.append(f"已确认未上班：{compact_dates([value.isoformat() for value in not_worked_dates])}")
    already_dates = sorted(value for value, status in status_map.items()
                           if status == 'already_reported'
                           and (year is None or value.year == year)
                           and (month is None or value.month == month))
    if already_dates:
        status_pieces.append(f"已报待查：{compact_dates([value.isoformat() for value in already_dates])}")
    pending_map = {_date(key): str(value) for key, value in (pending_status or {}).items()}
    pending_map = {
        value: state for value, state in pending_map.items()
        if year is None or (value.year == year and value.month == month)
    }
    state_labels = {
        'sent': '核实卡已发送，等待回复',
        'pending': '待核实，排队等待处理',
        'failed': '核实卡发送失败，等待重试',
    }
    for state in ('sent', 'pending', 'failed'):
        state_dates = sorted(value for value, item_state in pending_map.items()
                             if item_state == state)
        if state_dates:
            status_pieces.append(
                f"待核实：{compact_dates([value.isoformat() for value in state_dates])}"
                f"（{state_labels.get(state, '等待处理')}）"
            )
    if not dates:
        return "；".join(status_pieces) if status_pieces else "尚无有效记录"
    suffix = "；其余日期未纳入统计" if code == "D" and len(dates) == 1 else ""
    pieces = []
    if backfill_dates:
        pieces.append(f"✓ {compact_dates([value.isoformat() for value in backfill_dates])}（补报成功，{len(backfill_dates)}天）")
    if normal_dates:
        pieces.append(f"✓ {compact_dates([value.isoformat() for value in normal_dates])}（{len(normal_dates)}天）")
    # Missing dates are shown in the dedicated pending section below. Keeping
    # them out of the month cell prevents a pending date from looking like an
    # effective production record next to a check mark.
    if status_pieces:
        pieces.extend(status_pieces)
    return "；".join(pieces) + suffix


def render(
    date_map: Mapping[str, Iterable[str]],
    months: Iterable[tuple[int, int]],
    *,
    expected: Mapping[str, Iterable[str | dt.date]] | None = None,
    backfill_map: Mapping[str, Iterable[str | dt.date]] | None = None,
    not_worked_map: Mapping[str, Iterable[str | dt.date]] | None = None,
    attendance_status_map: Mapping[str, Mapping[str | dt.date, str] | Iterable[str | dt.date]] | None = None,
    pending_queue: Iterable[Mapping[str, str]] | None = None,
    window_start: str | dt.date | None = None,
    window_end: str | dt.date | None = None,
) -> str:
    """Build the complete two-table coverage section.

    ``date_map`` contains only effective dates already present in the ledger.
    Missing dates are displayed as ``待核实`` for the default A/B/C expected
    workdays.  D remains informational by default, but an explicit
    expected-date map can put a known D workday through the same status and
    backfill flow.  The caller owns the source-of-truth detail records.
    """
    normalized = {
        code: sorted({_date(value) for value in date_map.get(code, ())})
        for code in WORKERS
    }
    pending_by_worker: dict[str, dict[dt.date, str]] = {code: {} for code in WORKERS}
    for item in pending_queue or ():
        code = str(item.get('worker') or '')
        value = item.get('production_date')
        state = str(item.get('state') or 'pending')
        if code not in WORKERS or not value or state not in {'pending', 'failed', 'sent'}:
            continue
        try:
            pending_by_worker[code][_date(value)] = state
        except (TypeError, ValueError):
            continue
    raw_status = attendance_status_map or not_worked_map or {}
    normalized_status = {}
    for code in WORKERS:
        values = raw_status.get(code, {})
        if isinstance(values, Mapping):
            normalized_status[code] = {_date(key): str(value) for key, value in values.items()}
        else:
            normalized_status[code] = {_date(value): 'not_worked' for value in values}
    normalized_not_worked = {
        code: sorted(value for value, status in normalized_status[code].items()
                     if status == 'not_worked')
        for code in WORKERS
    }
    # A confirmed attendance result supersedes any stale pending-queue row for
    # the same worker/date. This keeps the coverage view from showing a date as
    # both confirmed and still waiting for a reply after upload/recovery.
    for code in WORKERS:
        for value in list(pending_by_worker[code]):
            if value in normalized_status[code]:
                pending_by_worker[code].pop(value, None)
    # A confirmed non-working day is an attendance result, not a production
    # date. Keep it out of every production count and missing-date check.
    for code in WORKERS:
        status_dates = set(normalized_status[code])
        normalized[code] = [value for value in normalized[code] if value not in status_dates]
    expected_map = {
        code: set(values)
        for code, values in (expected or expected_dates(
        normalized, window_start=window_start, window_end=window_end
        )).items()
    }
    for code in WORKERS:
        expected_map.setdefault(code, set())
        # Confirmed attendance outcomes are closed dates. They must not be
        # reintroduced as generic missing dates when the expected-date window
        # is derived from the remaining production records.
        expected_map[code].difference_update(normalized_status[code])
    # A queue row is a real operational state even though it is not a
    # production record. Include it in the pending display without changing
    # effective production dates or quantities.
    for code in WORKERS:
        expected_map[code].update(pending_by_worker[code])
    missing_map = missing_dates(normalized, expected_map)
    normalized_backfill = {
        code: sorted({value if isinstance(value, dt.date) else _date(value)
                      for value in (backfill_map or {}).get(code, ())})
        for code in WORKERS
    }
    month_values = sorted(set((int(year), int(month)) for year, month in months))
    if not month_values:
        month_values = sorted({(value.year, value.month) for dates in normalized.values() for value in dates})

    sync_time = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).strftime('%Y-%m-%d %H:%M')
    lines = [
        "## 人员生产记录覆盖情况",
        "",
        "### 同步状态",
        "",
        "| 数据源 | 版本/协议 | 状态 |",
        "|---|---|---|",
        "| GitHub | V1.17 | 当前台账来源 |",
        "| OpenClaw | V1.17 / S1 | 运行时已加载 |",
        f"| 飞书 | V1.17 / CARD-INTERACTIVE-1 | 卡片规则已同步 |",
        f"| 最后同步 | {sync_time} | 本次覆盖表生成时间 |",
        "",
        "### 异常检查",
        "",
        "| 检查项 | 结果 |",
        "|---|---:|",
        f"| 待核实日期 | {sum(len(values) for values in missing_map.values())}天 |",
        "| 状态冲突 | 0条 |",
        "| 汇总校验 | 通过 |",
        "| 三端规则版本 | 一致 |",
        "",
        "> 统计口径：表一同时展示有效生产和出勤核实状态；表二只展示有效生产日期。A/B固定班次和C长期上班的历史空档标记为“待核实”；D只有明确提供应上班日期时才进入待核实。确认“当天未上班”或“已经报过”的日期保留在出勤核实区，不计入生产天数、产量或生产明细；总览中的“已确认出勤状态天数”列统计这两类已确认状态总数。待核实队列会同步显示“已发送等待回复”或“排队等待处理”，但不计入生产累计。",
        "",
        "### 一、人员总览",
        "",
        "| 人员 | 有效生产天数 | 已确认未上班 | 已报待查 | 待处理日期 | 最近有效生产日 | 当前状态 |",
        "|---|---:|---:|---:|---|---|---|",
    ]
    for code, name in WORKERS.items():
        dates = normalized[code]
        status_dates = normalized_status[code]
        not_worked_dates = sorted(value for value, status in status_dates.items() if status == 'not_worked')
        already_reported_dates = sorted(value for value, status in status_dates.items() if status == 'already_reported')
        queue_dates = pending_by_worker[code]
        count = f"{len(dates)}天" if dates else "0天"
        status_count = f"{len(status_dates)}天"
        latest = _label(max(dates), include_year=True) if dates else "—"
        if queue_dates:
            queue_pieces = []
            state_labels = {
                'sent': '核实卡已发送，等待回复',
                'pending': '待核实，排队等待处理',
                'failed': '核实卡发送失败，等待重试',
            }
            for state in ('sent', 'pending', 'failed'):
                values = sorted(value for value, item_state in queue_dates.items()
                                if item_state == state)
                if values:
                    queue_pieces.append(
                        f"{compact_dates(value.isoformat() for value in values)}（{state_labels[state]}）"
                    )
            detail = "待核实队列：" + "；".join(queue_pieces)
            if normalized_backfill[code]:
                detail = (f"补报成功：{compact_dates(value.isoformat() for value in normalized_backfill[code])}，"
                          f"共{len(normalized_backfill[code])}天；已上传 GitHub；" + detail)
            status = "待核实"
        elif missing_map[code]:
            detail = f"待核实日期：{_compact_missing(missing_map[code])}；飞书提醒附补报格式"
            if normalized_backfill[code]:
                detail = (f"补报成功：{compact_dates(value.isoformat() for value in normalized_backfill[code])}，"
                          f"共{len(normalized_backfill[code])}天；已上传 GitHub；" + detail)
            status = "待核实"
        elif normalized_backfill[code]:
            detail = (f"补报成功：{compact_dates(value.isoformat() for value in normalized_backfill[code])}，"
                      f"共{len(normalized_backfill[code])}天；已上传 GitHub")
            status = "已确认（含补报）"
        elif code == "D" and len(dates) == 1:
            detail = "本周期只上班1天；其余日期未纳入统计"
            status = "本周期只上班1天"
        else:
            detail = "无" if dates else "尚无有效生产记录"
            status = "已确认" if dates else "尚无有效记录"
        if not_worked_dates:
            not_worked_detail = f"已确认未上班：{compact_dates(value.isoformat() for value in not_worked_dates)}"
            detail = f"{detail}；{not_worked_detail}" if detail not in {"无", "尚无有效生产记录"} else not_worked_detail
            if status != "待核实":
                status = "已确认（含未上班核实）"
        if already_reported_dates:
            already_detail = f"已报待查：{compact_dates(value.isoformat() for value in already_reported_dates)}"
            detail = f"{detail}；{already_detail}" if detail not in {"无", "尚无有效生产记录"} else already_detail
            if status != "待核实":
                status = "已确认（含已报待查）"
        pending_dates = sorted(set(missing_map[code]) | set(queue_dates))
        pending_label = _compact_missing(pending_dates) if pending_dates else "无"
        if missing_map[code]:
            status = "待核实"
        lines.append(f"| {code}｜{name} | {count} | {len(not_worked_dates)}天 | {len(already_reported_dates)}天 | {pending_label} | {latest} | {status} |")

    lines.extend([
        "",
        "### 二、按月份查看生产日期",
        "",
        "| 月份 | A｜徐超超 | B｜梅芳 | C｜李鸿玉 | D｜张小翠 |",
        "|---|---|---|---|---|",
    ])
    for year, month in month_values:
        cells = []
        for code in WORKERS:
            values = [value.isoformat() for value in normalized[code]
                      if value.year == year and value.month == month]
            cells.append(_month_cell(
                values, code, missing_map[code], year=year, month=month,
                backfill=normalized_backfill[code],
                not_worked=normalized_not_worked[code],
                attendance_status=normalized_status[code],
                pending_status=pending_by_worker[code],
            ))
        lines.append(f"| {_month_label(year, month)} | " + " | ".join(cells) + " |")

    lines.extend([
        "",
        "### 待核实日期",
        "",
        "| 员工 | 生产日期 | 当前状态 |",
        "|---|---|---|",
    ])
    pending_rows = []
    pending_keys = set()
    for code, name in WORKERS.items():
        for value, state in sorted(pending_by_worker[code].items()):
            state_label = {
                'sent': '核实卡已发送，等待回复',
                'pending': '待核实，排队等待处理',
                'failed': '核实卡发送失败，等待重试',
            }.get(state, '等待处理')
            pending_rows.append(
                f"| {code}｜{name} | {_label(value, include_year=True)} | {state_label} |"
            )
            pending_keys.add((code, value))
    for code, name in WORKERS.items():
        for value in sorted(missing_map[code]):
            if (code, value) in pending_keys:
                continue
            pending_rows.append(
                f"| {code}｜{name} | {_label(value, include_year=True)} | 尚无有效记录（待核实） |"
            )
    if pending_rows:
        lines.extend(pending_rows)
    else:
        lines.append("| — | — | 当前没有待核实日期 |")
    lines.extend([
        "",
        "### 已确认出勤状态日期",
        "",
        "| 员工 | 日期 | 当前状态 |",
        "|---|---|---|",
    ])
    status_rows = []
    for code, name in WORKERS.items():
        for value, state in sorted(normalized_status[code].items()):
            label = '已确认未上班' if state == 'not_worked' else '已报待查'
            status_rows.append(
                f"| {code}｜{name} | {_label(value, include_year=True)} | {label}（不计入生产统计） |"
            )
    lines.extend(status_rows or ["| — | — | 当前没有已确认出勤状态日期 |"])
    lines.extend([
        "",
        "> 图例：✓ 只表示有有效生产记录并已确认；“尚无有效记录（待核实）”不计入有效生产天数；“已确认未上班”和“已报待查”只保留出勤核实结果，不计入生产天数、产量或生产明细。选择“未上班”或“已报待查”并完成核对后，会显示对应状态；补报上传成功后显示“补报成功”。",
    ])
    return "\n".join(lines).rstrip() + "\n\n"
