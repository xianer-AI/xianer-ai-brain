"""Single canonical Feishu interactive-card contract for production review."""

from __future__ import annotations

import re

from quantity_display import quantity_note, summary_quantity_note

CARD_PROTOCOL = 'CARD-INTERACTIVE-1'
CARD_TITLE = '生产报数核对'
BACKFILL_REVIEW_TITLE = '生产补报数核对'
BACKFILL_TITLE = '补发生产日期待核实'
RECEIPT_TITLE = '上传成功回执'
BACKFILL_RECEIPT_TITLE = '补报上传成功回执'
STATUS_TITLE = '出勤状态核对'
_INSTRUCTION = ('请核实以上内容。明确报0或未上报项目均显示“数量为0，请核实”；'
                '无误请直接回复“**准确**”，核实项按0双上传；'
                '有错请回复“**错误**”，直接重报完整、正确的六项生产数量，缺项请明确填写0。'
                '原批次不会上传，系统会按新报数重新生成核对清单。')

_SUPPLEMENT_INSTRUCTION = ('请核对上面原生产日期的完整六项数量。已有记录保留，只补本次明确填写的缺项，不新增重复产量。'
                           '无误请回复“**准确**”；有错请回复“**错误**”，这张卡将停用后重新核对。'
                           '未填写的项目不能自动当作0，不能把历史补核当成今天的产能。')

_PRODUCTS = ('棉堆堆袜', '冰冰袜', '小腿袜', '过膝袜', '女船袜', '男船袜')
_WORKERS = {
    'A': ('徐超超', '下机'),
    'B': ('梅芳', '下机'),
    'C': ('李鸿玉', '烤边'),
    'D': ('张小翠', '烤边'),
}


def normalize_summary(summary):
    """Normalize old and new summaries to one employee-visible zero contract."""
    text = summary.rstrip()
    for product in _PRODUCTS:
        escaped = re.escape(product)
        text = re.sub(
            rf'(?m)^({escaped})\s*[：:]\s*(?:核实|未上报)\s*$',
            rf'\1：0双（数量为0，请核实）',
            text,
        )
        text = re.sub(
            rf'(?m)^({escaped})\s*[：:]\s*0\s*双?(?:（[^\n]*）)?\s*$',
            rf'\1：0双（数量为0，请核实）',
            text,
        )
    return text


def _decorate_backfill_summary(summary):
    """Highlight the explicit production date and backfill type on backfill cards."""
    lines = summary.splitlines()
    decorated = []
    has_type = any(line.strip().startswith('记录类型：') for line in lines)
    for line in lines:
        match = re.match(r'^(生产(?:日|日期)：)(?!\*\*)(.+?)(\*\*)?$', line)
        if match and '**' not in line:
            line = f'{match.group(1)}**{match.group(2).strip()}**'
            decorated.append(line)
            if not has_type:
                decorated.append('记录类型：**补报**')
                has_type = True
            continue
        decorated.append(line)
    if not has_type:
        decorated.insert(0, '记录类型：**补报**')
    return '\n'.join(decorated)


def payload(summary, *, backfill=False):
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError('核对卡内容不能为空')
    content = normalize_summary(summary)
    if backfill:
        content = _decorate_backfill_summary(content)
    note = summary_quantity_note(summary)
    if note:
        content = note + '\n\n' + content
    content += '\n\n' + (_SUPPLEMENT_INSTRUCTION if '历史缺项补核' in content else _INSTRUCTION)
    return {'config': {'wide_screen_mode': True},
            'header': {'title': {'tag': 'plain_text',
                                 'content': BACKFILL_REVIEW_TITLE if backfill else CARD_TITLE},
                       'template': 'blue'},
            'elements': [{'tag': 'div', 'text': {'tag': 'lark_md', 'content': content}}]}


def status_payload(summary, *, status='not_worked'):
    """Build a status-only card for choices 1/2.

    These choices are attendance/duplicate-record decisions.  They must not
    look like a six-product zero report and must never instruct an employee to
    upload quantities.  The later ``准确`` reply still goes through the same
    authenticated upload pipeline, where the status is written to coverage
    only.
    """
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError('状态核对卡内容不能为空')
    if status not in {'not_worked', 'already_reported'}:
        raise ValueError('未知出勤状态')
    title = '出勤状态核对' if status == 'not_worked' else '已报记录核实'
    instruction = (
        '请确认以上出勤状态。无误请直接回复“**准确**”；如果当天实际生产，请回复“**需要补报**”，系统会改发补报模板。'
        if status == 'not_worked' else
        '请确认该日期是否已经报过。确认原记录存在请回复“**准确**”；找不到原记录或需要补录请回复“**需要补报**”。'
    )
    content = summary.rstrip() + '\n\n' + instruction
    return {'config': {'wide_screen_mode': True},
            'header': {'title': {'tag': 'plain_text', 'content': title},
                       'template': 'blue'},
            'elements': [{'tag': 'div', 'text': {'tag': 'lark_md', 'content': content}}]}


def backfill_payload(summary):
    """Build a non-confirmation reminder with a copyable backfill template.

    This card intentionally does not include the normal “准确/错误” upload
    instruction: a reminder asks the employee to submit one dated report (or
    explicitly say “未上班/已报待查”), after which the ordinary review card is
    created by the existing inbox flow.
    """
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError('补报提醒内容不能为空')
    return {'config': {'wide_screen_mode': True},
            'header': {'title': {'tag': 'plain_text', 'content': BACKFILL_TITLE},
                       'template': 'orange'},
            'elements': [{'tag': 'div', 'text': {'tag': 'lark_md', 'content': summary.rstrip()}}]}


def backfill_input_payload(worker: str, production_date: str,
                           pending_dates: list[str] | None = None) -> dict:
    """Build the canonical manual/automatic fill-in card.

    Both entry points use this exact card so an employee always sees the same
    copyable six-line template.  Only the six quantities are editable; the
    employee, date, process and product labels stay fixed.
    """
    import datetime as dt

    if worker not in _WORKERS:
        raise ValueError('未知员工代号')
    day = dt.date.fromisoformat(str(production_date)).isoformat()
    name, process = _WORKERS[worker]
    lines = [
        '补报',
        f'{worker}={name}',
        f'生产日期：{day}',
        f'工序：{process}',
        *[f'{product}：' for product in _PRODUCTS],
    ]
    template = '\n'.join(lines)
    dates = []
    for value in pending_dates or []:
        normalized = dt.date.fromisoformat(str(value)).isoformat()
        if normalized not in dates:
            dates.append(normalized)
    if day not in dates:
        dates.insert(0, day)
    date_hint = []
    if len(dates) > 1:
        date_hint = [f'待补报日期共 **{len(dates)} 天**：' + '、'.join(dates),
                     '本次先处理当前日期；当前日期完成后，系统自动发送下一张。', '']
    content = '\n'.join([
        quantity_note(worker),
        '说明：保留下面模板中的原工序，只填写六项数字。',
        '',
        '请直接在六个产品名称后的冒号后填写数字，没有生产填0。',
        '员工、日期、工序和产品名称不要修改或删除；六项必须全部保留。',
        *date_hint,
        '',
        '```',
        template,
        '```',
        '',
        '填写完成后发送整段内容，系统会生成“生产补报数核对”卡，回复“准确”后才上传。',
        f'当天未上班请回复：未上班：{day}',
        f'已经报过请回复：已报待查：{day}',
    ])
    return {
        'config': {'wide_screen_mode': True},
        'header': {'title': {'tag': 'plain_text', 'content': '补报生产数据'},
                   'template': 'orange'},
        # A native markdown element keeps the fenced template as one
        # copyable code block in Feishu. No extra button or control is added.
        'elements': [{'tag': 'markdown', 'content': content}],
    }


def receipt_payload(message, *, backfill=False):
    """Wrap an already-rendered success receipt without changing its content."""
    if not isinstance(message, str) or not message.strip():
        raise ValueError('成功回执内容不能为空')
    return {'config': {'wide_screen_mode': True},
            'header': {'title': {'tag': 'plain_text',
                                 'content': BACKFILL_RECEIPT_TITLE if backfill else RECEIPT_TITLE},
                       'template': 'blue'},
            'elements': [{'tag': 'div', 'text': {'tag': 'lark_md', 'content': message}}]}


def envelope(summary):
    import json
    return {'msg_type': 'interactive',
            'content': json.dumps(payload(summary), ensure_ascii=False),
            'card_protocol': CARD_PROTOCOL}
