"""Read-only projection of one production batch's durable state."""
import hashlib
import json
import argparse
import sys
import time
import re
from pathlib import Path

import queue_store as q
import review_cards

VERSION_PATH = Path(__file__).with_name('VERSION.json')


def _version_info():
 try:
  data = json.loads(VERSION_PATH.read_text(encoding='utf-8'))
 except (FileNotFoundError, OSError, ValueError):
  data = {}
 return {
  'workbench_version': str(data.get('workbench_version') or 'V1.15'),
  'sync_protocol_version': str(data.get('sync_protocol_version') or 'S1'),
  'card_protocol_version': str(data.get('card_protocol_version') or 'CARD-INTERACTIVE-1'),
 }


VERSION_INFO = _version_info()
WORKBENCH_VERSION = VERSION_INFO['workbench_version']


def _verified_receipt(db, source):
    path = Path(db).parent / 'receipts' / (hashlib.sha256(str(source).encode()).hexdigest() + '.json')
    try:
        receipt = json.loads(path.read_text())
    except (FileNotFoundError, OSError, ValueError):
        return None
    if (receipt.get('status') == 'verified' and receipt.get('source') == source
            and receipt.get('commit')):
        return receipt
    return None


def _card(db, source):
    with q.conn(db) as conn:
        row = conn.execute(
            'SELECT * FROM review_cards WHERE source=? ORDER BY rowid DESC LIMIT 1',
            (source,),
        ).fetchone()
    return dict(row) if row else None


def _reply_delivery(db, source):
    """Return the durable success-reply state for this source, if tracked."""
    with q.conn(db) as conn:
        row = conn.execute(
            '''SELECT status,reply_message_id,error,attempts FROM confirmation_receipts
               WHERE source=?
               ORDER BY CASE WHEN status IN
                 ('pending','pending_dispatch','dispatching','dispatched')
                 THEN 0 ELSE 1 END, created DESC LIMIT 1''',
            (source,),
        ).fetchone()
    return dict(row) if row else None


def _upload_failure_status(reply_delivery):
    """Project a blocked upload into safe employee-facing wording.

    A blocked confirmation is already authenticated and must never be
    presented as a reconciliation request.  Keep the durable error for the
    operator while exposing a short, actionable reason to the employee.
    """
    error = str((reply_delivery or {}).get('error') or '').strip()
    if re.search(r'台账|个人累计|月度汇总|每日汇总|月份结构', error):
        return (
            '已确认，上传因台账月份结构失败',
            'ledger_structure',
        )
    return ('已确认，上传失败，系统已保留记录，管理员处理中', 'upload_failed')


def get_batch_status(db, source_id):
    """Return a stable, user-facing-safe status without mutating local or remote state."""
    inbox = q.get(db, source_id)
    if not inbox:
        return {'source': source_id, **VERSION_INFO,
                'state': 'not_found', 'reason': '找不到原始报数'}

    extracted = inbox.get('result', {}).get('extracted', {}) if inbox.get('result') else {}

    receipt = _verified_receipt(db, source_id)
    card = _card(db, source_id)
    reply_delivery = _reply_delivery(db, source_id)
    delivery = card.get('delivery') if card else None
    card_state = card.get('state') if card else None

    if reply_delivery and reply_delivery['status'] == 'blocked':
        reason, failure_kind = _upload_failure_status(reply_delivery)
        return {
                'source': source_id,
                **VERSION_INFO,
                'production_date': extracted.get('production_date'),
                'worker': extracted.get('worker'),
                'state': 'upload_failed',
                'card_state': card_state,
                'delivery_state': delivery,
                'reply_delivery': 'blocked',
                'reply_message_id': reply_delivery.get('reply_message_id'),
                'upload_state': 'failed',
                'failure_kind': failure_kind,
                'error': reply_delivery.get('error'),
                'attempts': reply_delivery.get('attempts'),
                'reason': reason,
            }
    if receipt:
        if reply_delivery and reply_delivery['status'] == 'reply_blocked':
            return {
                'source': source_id,
                **VERSION_INFO,
                'production_date': extracted.get('production_date'),
                'worker': extracted.get('worker'),
                'state': 'needs_reconciliation',
                'card_state': card_state,
                'delivery_state': delivery,
                'reply_delivery': 'reply_blocked',
                'reply_message_id': reply_delivery.get('reply_message_id'),
                'upload_state': 'verified_reply_blocked',
                'commit': receipt['commit'],
                'reason': 'GitHub 已验证，但飞书成功回执连续失败，需要人工补发回执',
            }
        if reply_delivery and reply_delivery['status'] != 'dispatched':
            return {
                'source': source_id,
                'production_date': extracted.get('production_date'),
                'worker': extracted.get('worker'),
                'state': 'reply_pending',
                'card_state': card_state,
                'delivery_state': delivery,
                'reply_delivery': reply_delivery['status'],
                'reply_message_id': reply_delivery.get('reply_message_id'),
                'upload_state': 'verified_reply_pending',
                'commit': receipt['commit'],
                'reason': 'GitHub 已验证，等待飞书成功回执确认',
            }
        return {
            'source': source_id,
            **VERSION_INFO,
            'production_date': extracted.get('production_date'),
            'worker': extracted.get('worker'),
            'state': 'completed',
            'card_state': card_state,
            'delivery_state': delivery,
            'reply_delivery': reply_delivery['status'] if reply_delivery else None,
            'reply_message_id': reply_delivery.get('reply_message_id') if reply_delivery else None,
            'upload_state': 'verified',
            'commit': receipt['commit'],
            'reason': 'GitHub 已提交并完成远程回读',
        }
    if delivery == 'unknown':
        return {
                'source': source_id,
                **VERSION_INFO,
            'production_date': extracted.get('production_date'),
            'worker': extracted.get('worker'),
            'state': 'needs_reconciliation',
            'card_state': card_state,
            'delivery_state': 'unknown',
            'upload_state': 'not_started',
            'reason': '飞书核对卡送达结果不明确，需要先核实',
        }
    if card_state in ('pending', 'confirmed'):
        state = 'awaiting_confirmation' if card_state == 'pending' else 'confirmed'
        upload = 'not_started' if card_state == 'pending' else 'queued'
        return {
            'source': source_id,
            **VERSION_INFO,
            'production_date': extracted.get('production_date'),
            'worker': extracted.get('worker'),
            'state': state,
            'card_state': card_state,
            'delivery_state': delivery,
            'upload_state': upload,
            'reason': '等待员工确认' if card_state == 'pending' else '已确认，等待上传',
        }
    return {
        'source': source_id,
        **VERSION_INFO,
        'production_date': extracted.get('production_date'),
        'worker': extracted.get('worker'),
        'state': 'received' if inbox.get('status') != 'ready' else 'parsed',
        'card_state': card_state,
        'delivery_state': delivery,
        'upload_state': 'not_started',
        'reason': '已收到，尚未生成有效核对卡',
    }


def latest_actionable_batch(db, sender, group):
    """Return newest employee-owned batch requiring action, if one exists."""
    review_cards.init(db)
    now = time.time()
    with q.conn(db) as conn:
        rows = conn.execute(
            '''SELECT source FROM review_cards
               WHERE sender=? AND grp=? AND expires>? AND state IN ('pending','confirmed')
               ORDER BY rowid DESC''',
            (sender, group, now),
        ).fetchall()
    for row in rows:
        status = get_batch_status(db, row['source'])
        if status['state'] in ('awaiting_confirmation', 'confirmed', 'needs_reconciliation', 'reply_pending', 'upload_failed'):
            return status
    return None


def _main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=('status', 'latest'))
    parser.add_argument('--db', default=None)
    parser.add_argument('--source')
    parser.add_argument('--sender')
    parser.add_argument('--group', default=review_cards.GROUP)
    args = parser.parse_args(argv)
    db = args.db or str(Path.home() / '.openclaw/state/production-parallel/inbox.sqlite')
    if args.command == 'status':
        if not args.source:
            parser.error('--source is required for status')
        result = get_batch_status(db, args.source)
    else:
        if not args.sender:
            parser.error('--sender is required for latest')
        result = latest_actionable_batch(db, args.sender, args.group)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    _main()
