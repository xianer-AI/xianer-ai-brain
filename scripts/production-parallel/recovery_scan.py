"""Recover missed review cards after gateway/provider recovery.

This module only creates durable pending cards. It never confirms or writes GitHub.
"""
import json
import re
import os
import time
import secrets
import hashlib
import urllib.error
from pathlib import Path

import queue_store as q
import card_builder

GROUP = 'oc_1f8587b1bcde12a0d1bb6053ab2b748a'
PRODUCTS = ['棉堆堆袜', '冰冰袜', '小腿袜', '过膝袜', '女船袜', '男船袜']
# For cards that were accepted by Feishu but never confirmed by the employee:
# remind once after five minutes, then once more thirty minutes later.
REMINDER_DELAYS = (300, 1800)

def _verified_upload_receipt(db, source):
    """A remotely verified source is complete, even if an old card is pending."""
    path = Path(db).parent / 'receipts' / (hashlib.sha256(str(source).encode()).hexdigest() + '.json')
    try:
        receipt = json.loads(path.read_text())
    except (FileNotFoundError, OSError, ValueError):
        return False
    return receipt.get('status') == 'verified' and receipt.get('source') == source and bool(receipt.get('commit'))

def delivery_allowed(db):
    """Allow real Feishu delivery only for the production database.

    Unit tests and recovery simulations use temporary SQLite files. They may
    inject a fake ``transport`` module for delivery assertions, but must never
    fall through to the live Feishu transport accidentally.
    """
    production_db = Path.home() / '.openclaw/state/production-parallel/inbox.sqlite'
    if os.path.realpath(str(db)) == os.path.realpath(str(production_db)):
        return True
    # Tests inject an in-memory module with no ``__file__`` so delivery can be
    # asserted without touching Feishu.  A real transport module is file
    # backed, so its mere import cannot authorize a temporary database.
    transport = __import__('sys').modules.get('transport')
    return transport is not None and getattr(transport, '__file__', None) is None


def _summary(record):
    event = record['event']
    result = record.get('result') or {}
    extracted = result.get('extracted') or {}
    if extracted.get('status_only') or extracted.get('attendance_status'):
        status = extracted.get('attendance_status')
        label = '当天未上班' if status == 'not_worked' else '已经报过'
        identity = None
        try:
            from worker_identity import lookup
            identity = lookup(record.get('_db', ''), record['sender'])
        except Exception:
            pass
        if identity:
            identity_line = f"身份：{identity['worker']}={identity['name']} 请核实"
        else:
            identity_line = f"身份：{extracted.get('worker') or '待核实'} 请核实"
        return '\n'.join([
            identity_line,
            f"生产日期：{extracted.get('production_date') or '待核实'}",
            f"出勤状态：{label}",
            '该日期不生成生产数量，不计入生产累计；请核实此状态。',
        ])
    items = {x.get('product'): x.get('quantity') for x in extracted.get('items', [])}
    identity = None
    try:
        from worker_identity import lookup
        identity = lookup(record.get('_db', ''), record['sender'])
    except Exception:
        pass
    if identity:
        identity_line = f"身份：{identity['worker']}={identity['name']} 请核实"
    elif extracted.get('proxy') and extracted.get('worker') in ('A', 'B', 'C', 'D'):
        names = {'A': '徐超超', 'B': '梅芳', 'C': '李鸿玉', 'D': '张小翠'}
        identity_line = f"老板代报：{extracted['worker']}={names[extracted['worker']]} 请核实"
    else:
        identity_line = '身份：待核实（不能据此自动归入 A/B/C/D）'
    label = '历史缺项补核（保留已有数量，不是今天产能）' if extracted.get('historical_supplement') else '请核对本次生产报数'
    lines = [label, identity_line,
             f"生产日：{extracted.get('production_date') or '待核实'}"]
    total = 0
    for product in PRODUCTS:
        if product in items:
            value = int(items[product])
            total += value
            suffix = ('（本次补核）' if product in extracted['historical_supplement'] else '（保留原记录）') if extracted.get('historical_supplement') else ''
            lines.append(f'{product}：{value} 双{suffix}')
        else:
            lines.append(f'{product}：0 双（数量为0，请核实）')
    lines.append(f'合计：{total} 双（已报项目小计）')
    lines.append(f"原始消息ID：{event['messageId']}")
    lines.append('当前状态：待核对，尚未确认，尚未上传 GitHub')
    return '\n'.join(lines)


def _ensure_table(db):
    q.init(db)
    with q.conn(db) as c:
        c.execute('''CREATE TABLE IF NOT EXISTS review_cards(
          token TEXT PRIMARY KEY, source TEXT, sender TEXT, grp TEXT,
          summary TEXT, digest TEXT, expires REAL, state TEXT,
          callback TEXT, result TEXT)''')
        for column, spec in (('delivery', "TEXT NOT NULL DEFAULT 'pending'"),
                             ('delivery_error', 'TEXT'), ('message_id', 'TEXT'),
                             ('delivery_attempts', 'INTEGER NOT NULL DEFAULT 0'),
                             ('delivery_next_at', 'REAL'),
                             ('delivery_history', "TEXT NOT NULL DEFAULT '[]'"),
                             ('sent_at', 'REAL'),
                             ('last_reminder_at', 'REAL'),
                             ('resend_count', 'INTEGER NOT NULL DEFAULT 0'),
                             ('backfill', 'INTEGER NOT NULL DEFAULT 0'),
                             ('card_kind', "TEXT NOT NULL DEFAULT 'production'"),
                             ('status_type', 'TEXT')):
            try: c.execute(f'ALTER TABLE review_cards ADD COLUMN {column} {spec}')
            except Exception: pass
        # Cards created by the previous version have a sent state but no
        # timestamp. Use their 24-hour expiry as the best available estimate
        # of the original send time so they can enter the reminder schedule.
        c.execute("""UPDATE review_cards
                     SET sent_at=COALESCE(sent_at, expires - 86400, ?)
                     WHERE delivery='sent' AND sent_at IS NULL""", (time.time(),))
        c.execute("UPDATE review_cards SET resend_count=0 WHERE resend_count IS NULL")
        c.execute("""UPDATE review_cards
                   SET delivery_next_at=COALESCE(delivery_next_at,?)
                   WHERE state='pending' AND delivery IN ('pending','failed')""", (time.time(),))
        c.execute('''CREATE TABLE IF NOT EXISTS recovery_suppressions(
          source TEXT PRIMARY KEY, reason TEXT, created REAL NOT NULL)''')

def suppress_source(db, source, reason):
    """Prevent a known historical source from being replayed by recovery."""
    _ensure_table(db)
    with q.conn(db) as c:
        c.execute('INSERT OR REPLACE INTO recovery_suppressions(source,reason,created) VALUES(?,?,?)',
                  (source, reason, time.time()))

def deliver(db):
    """Deliver pending cards and remind sent-but-unconfirmed cards."""
    _ensure_table(db)
    if not delivery_allowed(db):
        return []
    try:
        from transport import request
    except ImportError:
        # Unit tests and offline recovery can still build durable cards. The
        # service adds the companion transport path before calling this.
        return []
    with q.conn(db) as c:
        now = time.time()
        rows = c.execute("""SELECT * FROM review_cards
          WHERE state='pending' AND (
            (delivery IN ('pending','failed') AND
             COALESCE(delivery_next_at,0) <= ?) OR
            (delivery='sent' AND resend_count < ? AND
             ? - COALESCE(last_reminder_at, sent_at, 0) >=
               CASE resend_count WHEN 0 THEN ? WHEN 1 THEN ? ELSE 999999999 END)
          ) ORDER BY rowid""", (now, len(REMINDER_DELAYS), now, *REMINDER_DELAYS)).fetchall()
        for row in rows:
            try:
                from historical_supplement import validate_supplement_summary
                source=c.execute('SELECT * FROM inbox WHERE id=?',(row['source'],)).fetchone()
                if source: validate_supplement_summary(dict(source),row['summary'])
            except ValueError as exc:
                c.execute("UPDATE review_cards SET state='superseded',delivery_error=? WHERE token=?",(str(exc),row['token']))
                continue
            is_reminder = row['delivery'] == 'sent'
            resend_count = int(row['resend_count'] or 0)
            # A recalled Feishu card is an explicit operator decision. Close
            # the durable task before recovery can resend it; recalling the
            # visible message alone must never resurrect an old date/employee
            # task.
            if is_reminder and row['message_id']:
                try:
                    current = request('xiaowen', 'GET', f"/im/v1/messages/{row['message_id']}")
                    item = (current.get('data') or {}).get('items', [{}])[0]
                    if item.get('deleted'):
                        c.execute("UPDATE review_cards SET state='superseded', delivery_error=? WHERE token=?", ('Feishu card recalled; task closed', row['token']))
                        continue
                except Exception:
                    # An uncertain lookup must not close or resend the task.
                    continue
            if is_reminder and resend_count >= len(REMINDER_DELAYS):
                continue
            token = row['token'] if not is_reminder else f"{row['token']}:reminder:{resend_count + 1}"
            try:
                card=(card_builder.status_payload(row['summary'], status=row['status_type'] or 'not_worked')
                      if row['card_kind'] == 'status' else
                      card_builder.payload(row['summary'],backfill=bool(row['backfill'])))
                result=request('xiaowen','POST','/im/v1/messages?receive_id_type=chat_id',{'receive_id':row['grp'],'msg_type':'interactive','content':json.dumps(card,ensure_ascii=False),'uuid':token})
                now = time.time()
                message_id=result.get('data',{}).get('message_id')
                try: history=json.loads(row['delivery_history'] or '[]')
                except Exception: history=[]
                history.append({'message_id':message_id,'sent_at':now,'resend':is_reminder})
                if is_reminder:
                    c.execute("""UPDATE review_cards
                               SET delivery='sent', delivery_error=NULL,
                                   message_id=?, last_reminder_at=?,
                                   delivery_next_at=?, delivery_attempts=delivery_attempts+1,
                                   delivery_history=?, resend_count=? WHERE token=?""",
                              (message_id, now, now + REMINDER_DELAYS[1],
                               json.dumps(history,ensure_ascii=False), resend_count + 1, row['token']))
                else:
                    c.execute("""UPDATE review_cards
                               SET delivery='sent', delivery_error=NULL,
                                   message_id=?, sent_at=COALESCE(sent_at,?),
                                   delivery_next_at=?, delivery_attempts=delivery_attempts+1,
                                   delivery_history=?, resend_count=COALESCE(resend_count,0)
                               WHERE token=?""",
                              (message_id, now, now + REMINDER_DELAYS[0],
                               json.dumps(history,ensure_ascii=False), row['token']))
            except urllib.error.HTTPError as exc:
                detail = f'Feishu HTTP {exc.code}: {exc.reason}'
                # The platform explicitly rejected the request.  This is a
                # retryable delivery failure, not an unknown write outcome:
                # no message was accepted, so reconciliation is unnecessary.
                c.execute("""UPDATE review_cards SET delivery='failed',delivery_error=?,
                           delivery_attempts=delivery_attempts+1,delivery_next_at=?
                           WHERE token=?""", (detail[:180], time.time()+30, row['token']))
            except Exception as exc:
                detail=type(exc).__name__+': '+str(exc)[:180]
                # An explicit Feishu API error is retryable. A timeout or
                # transport exception has an unknown outcome and is held for
                # operator reconciliation instead of blind duplication.
                if isinstance(exc, RuntimeError):
                    c.execute("""UPDATE review_cards SET delivery='failed',delivery_error=?,
                               delivery_attempts=delivery_attempts+1,delivery_next_at=?
                               WHERE token=?""", (detail, time.time()+30, row['token']))
                else:
                    c.execute("""UPDATE review_cards SET delivery='unknown',delivery_error=?
                               WHERE token=?""", (detail, row['token']))


def scan(db, limit=200):
    """Create at most one pending recovery card per source message."""
    _ensure_table(db)
    created = []
    with q.conn(db) as c:
        # Apply the limit after filtering, otherwise processed older rows can
        # permanently starve newer reports once the ready backlog exceeds it.
        rows = c.execute("""SELECT * FROM inbox
          WHERE grp=? AND status='ready' ORDER BY created,id""",
                         (GROUP,)).fetchall()
        for row in rows:
            if c.execute('SELECT 1 FROM recovery_suppressions WHERE source=?', (row['id'],)).fetchone():
                continue
            if _verified_upload_receipt(db, row['id']):
                c.execute('INSERT OR REPLACE INTO recovery_suppressions(source,reason,created) VALUES(?,?,?)',
                          (row['id'], 'verified GitHub upload already exists', time.time()))
                continue
            existing = c.execute(
                "SELECT state FROM review_cards WHERE source=? ORDER BY rowid DESC LIMIT 1",
                (row['id'],)).fetchone()
            # A superseded card may be an intentional manual archive. Do not
            # recreate it on every recovery scan; a new card is created only
            # when a fresh source or an explicit review_cards.issue() call is
            # processed.
            if existing and existing['state'] in ('pending', 'confirmed', 'uploaded', 'modified', 'deferred', 'superseded', 'archived'):
                continue
            record = dict(row); record['_db'] = db
            record['event'] = json.loads(record['event'])
            record['result'] = json.loads(record['result']) if record['result'] else None
            extracted = (record['result'] or {}).get('extracted') or {}
            if extracted.get('kind') != 'report':
                continue
            # Explicit-date partial supplements require a bound merge preview,
            # never the normal missing-as-zero review path.
            supplied = {item.get('product') for item in extracted.get('items', [])}
            explicit_date = bool(re.search(r'20\d{2}[-年/]\d{1,2}[-月/]\d{1,2}', str(record['event'].get('content', ''))))
            if explicit_date and supplied and supplied != set(PRODUCTS):
                try:
                    from historical_supplement import merge_extracted
                    from worker_identity import lookup
                    import commit_guard, base64
                    identity = lookup(db, row['sender'])
                    endpoint = commit_guard.endpoint_for_date(extracted['production_date'])
                    remote = commit_guard.gh_read_json(endpoint)
                    text = base64.b64decode(remote['content']).decode('utf-8')
                    extracted = merge_extracted(text, extracted, identity)
                    # Keep the original parser output and raw message for audit.
                    record['result']['original_extracted'] = record['result']['extracted']
                    record['result']['extracted'] = extracted
                    c.execute('UPDATE inbox SET result=? WHERE id=?',
                              (json.dumps(record['result'], ensure_ascii=False), row['id']))
                except Exception as exc:
                    c.execute('UPDATE inbox SET error=? WHERE id=?',
                              ('历史补核暂停：' + str(exc)[:180], row['id']))
                    continue
            try:
                from historical_supplement import require_bound_supplement
                require_bound_supplement(record)
            except ValueError as exc:
                c.execute('UPDATE inbox SET error=? WHERE id=?', ('报数暂停：'+str(exc),row['id']))
                continue
            if extracted.get('historical_supplement'):
                duplicate = False
                for active in c.execute("SELECT rc.source,i.result FROM review_cards rc JOIN inbox i ON i.id=rc.source WHERE rc.sender=? AND rc.grp=? AND rc.state IN ('pending','confirmed') AND rc.source<>?", (row['sender'],row['grp'],row['id'])).fetchall():
                    other = (json.loads(active['result'] or '{}').get('extracted') or {})
                    if other.get('production_date') != extracted.get('production_date'):
                        continue
                    duplicate = True
                    if other.get('historical_supplement') == extracted['historical_supplement']:
                        c.execute('INSERT OR REPLACE INTO recovery_suppressions(source,reason,created) VALUES(?,?,?)', (row['id'],'duplicate historical supplement; bound to '+active['source'],time.time()))
                    else:
                        c.execute('UPDATE inbox SET error=? WHERE id=?', ('历史补核暂停：同一天已有待处理核对卡，不能并行确认不同版本',row['id']))
                    break
                if duplicate:
                    continue
            # Empty model drafts are not production reports. They used to
            # create noisy all-"核实" cards with a zero subtotal (including
            # test fixture source IDs). A real all-zero report still has
            # explicit item rows and remains eligible.
            if not (extracted.get('items') or []):
                continue
            token = secrets.token_hex(6)
            card_kind = 'status' if extracted.get('status_only') or extracted.get('attendance_status') else 'production'
            status_type = extracted.get('attendance_status')
            c.execute('INSERT INTO review_cards(token,source,sender,grp,summary,digest,expires,state,callback,result,backfill,card_kind,status_type) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                      (token, row['id'], row['sender'], row['grp'], _summary(record),
                       row['digest'], time.time() + 86400, 'pending', None, None,
                       1 if extracted.get('backfill') else 0, card_kind, status_type))
            created.append(row['id'])
    deliver(db)
    return created


if __name__ == '__main__':
    import argparse
    from service import DB
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', default=DB)
    args = parser.parse_args()
    print(json.dumps({'created': scan(args.db)}, ensure_ascii=False))
