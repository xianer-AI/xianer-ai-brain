"""Durable employee-bound review cards; native authenticated callback required."""
import json,secrets,time,argparse,sys,re,os,subprocess
import urllib.error
from pathlib import Path
import queue_store as q
import card_builder
GROUP='oc_1f8587b1bcde12a0d1bb6053ab2b748a'
OWNER='ou_de130236fb86ee826e0f5653f05bc9c6'
# Upload failures must never invalidate an employee's confirmed batch.  The
# attempt count remains useful for diagnostics, but it is no longer a hard
# stop: a remote SHA race, gateway restart, or temporary GitHub failure can be
# recovered without asking the employee to confirm again.
MAX_DISPATCH_ATTEMPTS = 3
MAX_REPLY_ATTEMPTS = 3
DISPATCH_RETRY_BASE_SECONDS = 30
DISPATCH_RETRY_MAX_SECONDS = 1800


def _dispatch_retry_delay(attempts):
 """Return a bounded exponential retry delay for an upload attempt."""
 try:
  count = max(1, int(attempts or 1))
 except (TypeError, ValueError):
  count = 1
 return min(DISPATCH_RETRY_MAX_SECONDS,
            DISPATCH_RETRY_BASE_SECONDS * (2 ** min(count - 1, 6)))

def _verified_upload_receipt(db, source, confirmation=None):
 """Return whether this exact source already has a verified GitHub upload."""
 import hashlib
 path=Path(db).parent/'receipts'/(hashlib.sha256(str(source).encode()).hexdigest()+'.json')
 try:
  receipt=json.loads(path.read_text())
 except (FileNotFoundError, OSError, ValueError):
  return False
 return (receipt.get('status')=='verified' and receipt.get('source')==source
         and (confirmation is None or receipt.get('confirmation')==confirmation)
         and bool(receipt.get('commit')))

def ready_report(record):
 result=record.get('result') or {}
 extracted=result.get('extracted') or {}
 items=extracted.get('items') or []
 return record.get('status')=='ready' and extracted.get('kind')=='report' and bool(items)
def init(db):
 q.init(db)
 with q.conn(db) as c:
  c.execute('CREATE TABLE IF NOT EXISTS review_cards(token TEXT PRIMARY KEY,source TEXT,sender TEXT,grp TEXT,summary TEXT,digest TEXT,expires REAL,state TEXT,callback TEXT,result TEXT)')
  for column, spec in (('delivery', "TEXT NOT NULL DEFAULT 'pending'"),
                       ('delivery_error', 'TEXT'), ('message_id', 'TEXT'),
                       ('delivery_attempts', 'INTEGER NOT NULL DEFAULT 0'),
                       ('delivery_next_at', 'REAL'),
                       ('last_delivery_at', 'REAL'),
                       ('resend_count', 'INTEGER NOT NULL DEFAULT 0'),
                       ('delivery_history', "TEXT NOT NULL DEFAULT '[]'"),
                       ('sent_at', 'REAL'),
                       ('last_reminder_at', 'REAL'),
                       ('confirmation_retry_status', 'TEXT'),
                       ('confirmation_text', 'TEXT'),
                       ('confirmation_message_id', 'TEXT'),
                       ('confirmation_error', 'TEXT'),
                       ('confirmation_attempts', 'INTEGER NOT NULL DEFAULT 0'),
                       ('backfill', 'INTEGER NOT NULL DEFAULT 0'),
                       ('card_kind', "TEXT NOT NULL DEFAULT 'production'"),
                       ('status_type', 'TEXT'),
                       ('worker_pid', 'INTEGER')):
   try: c.execute(f'ALTER TABLE review_cards ADD COLUMN {column} {spec}')
   except Exception: pass
  c.execute('''CREATE TABLE IF NOT EXISTS confirmation_receipts(
    message_id TEXT PRIMARY KEY, sender TEXT NOT NULL, grp TEXT NOT NULL,
    text TEXT NOT NULL, source TEXT, token TEXT, status TEXT NOT NULL,
    error TEXT, error_detail TEXT, attempts INTEGER NOT NULL DEFAULT 0,
    next_at REAL NOT NULL, created REAL NOT NULL)''')
  try: c.execute('ALTER TABLE confirmation_receipts ADD COLUMN error_detail TEXT')
  except Exception: pass
  try: c.execute('ALTER TABLE confirmation_receipts ADD COLUMN worker_pid INTEGER')
  except Exception: pass
  try: c.execute('ALTER TABLE confirmation_receipts ADD COLUMN reply_message_id TEXT')
  except Exception: pass
  try: c.execute('ALTER TABLE confirmation_receipts ADD COLUMN dispatch_started_at REAL')
  except Exception: pass
  try: c.execute('ALTER TABLE confirmation_receipts ADD COLUMN dispatched_at REAL')
  except Exception: pass
  try: c.execute('ALTER TABLE confirmation_receipts ADD COLUMN dispatch_duration_ms REAL')
  except Exception: pass
  try: c.execute('ALTER TABLE confirmation_receipts ADD COLUMN reply_attempts INTEGER NOT NULL DEFAULT 0')
  except Exception: pass
  # Receipts created by the old bounded policy could be left permanently
  # blocked after three transient failures. Requeue them once; the durable
  # source/confirmation binding remains authoritative.
  c.execute("""UPDATE confirmation_receipts
    SET status='pending_dispatch',
        error=COALESCE(error,'旧版上传重试已恢复'),
        error_detail=COALESCE(error_detail,'旧版三次上限策略遗留，已自动恢复重试'),
        next_at=?
    WHERE status='blocked' AND source IS NOT NULL AND token IS NOT NULL""",
    (time.time(),))
  # A pre-upgrade process may have left a non-blocked receipt at the old
  # limit. Mark it as migrated so this repair runs only once per row.
  c.execute("""UPDATE confirmation_receipts
    SET status='pending_dispatch',
        error_detail='旧版三次上限策略遗留，已自动恢复重试',
        next_at=?
    WHERE status IN ('pending','pending_dispatch') AND attempts>=?
      AND (error_detail IS NULL OR error_detail='')""",
    (time.time(), MAX_DISPATCH_ATTEMPTS))
  # Active cards may have been created by the pre-V1.4 formatter. Normalize
  # only pending/confirmed cards so a retry cannot resend the old missing-text
  # shape; completed/superseded history remains unchanged for auditability.
  for row in c.execute("SELECT token,summary FROM review_cards WHERE state IN ('pending','confirmed')").fetchall():
   normalized=card_builder.normalize_summary(row['summary'] or '')
   if normalized != (row['summary'] or ''):
    c.execute('UPDATE review_cards SET summary=? WHERE token=?',(normalized,row['token']))

def mark_card_delivery_failed(db, token, error, now=None):
 """Persist an explicit Feishu delivery failure for the 30-second retry scan."""
 init(db); now=time.time() if now is None else now
 with q.conn(db) as c:
  c.execute("""UPDATE review_cards
    SET delivery='failed', delivery_error=?, delivery_attempts=delivery_attempts+1,
        delivery_next_at=? WHERE token=? AND state='pending'""",
    (str(error)[:240], now+30, token))

def mark_card_delivery_unknown(db, token, error, now=None):
 """Persist an uncertain platform outcome without scheduling a blind resend."""
 init(db); now=time.time() if now is None else now
 with q.conn(db) as c:
  c.execute("""UPDATE review_cards
    SET delivery='unknown', delivery_error=?, delivery_next_at=NULL
    WHERE token=? AND state='pending'""", (str(error)[:240], token))

def mark_card_delivery_sent(db, token, message_id, now=None, is_resend=False):
 """Persist a successful Feishu receipt and schedule the next resend window."""
 init(db); now=time.time() if now is None else now
 delay=1800 if is_resend else 300
 with q.conn(db) as c:
  row=c.execute('SELECT delivery_history FROM review_cards WHERE token=?',(token,)).fetchone()
  try: history=json.loads(row['delivery_history'] or '[]') if row else []
  except Exception: history=[]
  history.append({'message_id':str(message_id),'sent_at':now,'resend':bool(is_resend)})
  c.execute("""UPDATE review_cards SET delivery='sent', delivery_error=NULL,
      message_id=?, last_delivery_at=?, sent_at=COALESCE(sent_at,?),
      last_reminder_at=CASE WHEN ? THEN ? ELSE last_reminder_at END,
      delivery_next_at=?,
      delivery_attempts=delivery_attempts+1,
      resend_count=resend_count+?, delivery_history=?
      WHERE token=? AND state='pending'""",
    (str(message_id), now, now, 1 if is_resend else 0, now, now+delay, 1 if is_resend else 0,
     json.dumps(history,ensure_ascii=False), token))

def pending_card_deliveries(db, now=None):
 """Return unsent/explicitly failed card sends whose retry window is due."""
 init(db); now=time.time() if now is None else now
 with q.conn(db) as c:
  return [dict(r) for r in c.execute("""SELECT * FROM review_cards
    WHERE state='pending' AND delivery IN ('pending','failed') AND expires>? AND
          delivery_next_at IS NOT NULL AND delivery_next_at<=?
    ORDER BY rowid""", (now, now)).fetchall()]

def pending_card_resends(db, now=None):
 """Return sent, unconfirmed cards due for one of the two scheduled resends."""
 init(db); now=time.time() if now is None else now
 with q.conn(db) as c:
  return [dict(r) for r in c.execute("""SELECT * FROM review_cards
    WHERE state='pending' AND delivery='sent' AND expires>? AND
          resend_count<2 AND delivery_next_at IS NOT NULL AND delivery_next_at<=?
    ORDER BY rowid""", (now, now)).fetchall()]

def _queue_confirmation_receipt(c, message_id, sender, group, text, source, token):
 """Persist every valid confirmation before handing work to an agent.

 The receipt is the hand-off boundary: if the gateway or model provider is
 unavailable after the employee confirms, the 30-second recovery loop can
 retry the same source/confirmation pair without asking the employee to
 confirm again or creating another production batch.
 """
 now=time.time()
 c.execute("""INSERT INTO confirmation_receipts
   (message_id,sender,grp,text,source,token,status,error,attempts,next_at,created)
   VALUES(?,?,?,?,?,?,?,?,?,?,?)
   ON CONFLICT(message_id) DO UPDATE SET source=excluded.source,
     token=excluded.token,status=CASE WHEN confirmation_receipts.status IN ('dispatched','blocked','reply_pending','reply_blocked')
       THEN confirmation_receipts.status ELSE 'pending_dispatch' END,
     error=NULL,next_at=excluded.next_at""",
  (message_id,sender,group,text,source,token,'pending_dispatch',None,0,now,now))

def record_failed_confirmation(db,text,sender,group,callback,error):
 """Persist a confirmation that reached the gateway but could not finish."""
 if not re.fullmatch(r'(?:准确|确认|确认上传)(?:\s+om_[A-Za-z0-9_-]+)?', text.strip()):
  raise ValueError('不是确认消息')
 init(db)
 now=time.time()
 with q.conn(db) as c:
  card=c.execute("""SELECT token,source FROM review_cards
    WHERE sender=? AND grp=? AND state='confirmed' AND expires>?
    ORDER BY rowid DESC LIMIT 1""",(sender,group,now)).fetchone()
  c.execute("""INSERT INTO confirmation_receipts
    (message_id,sender,grp,text,source,token,status,error,error_detail,attempts,next_at,created)
    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
    ON CONFLICT(message_id) DO UPDATE SET status=CASE WHEN confirmation_receipts.status IN ('dispatched','blocked','reply_pending','reply_blocked')
      THEN confirmation_receipts.status ELSE 'pending_dispatch' END,
      error=excluded.error,next_at=excluded.next_at""",
   (callback,sender,group,text,card['source'] if card else None,
    card['token'] if card else None,'pending' if card else 'unmatched',str(error)[:240],str(error),0,now+30,now))

def pending_confirmations(db,now=None):
 init(db); now=time.time() if now is None else now
 with q.conn(db) as c:
  return [dict(r) for r in c.execute(
   """SELECT * FROM confirmation_receipts
      WHERE (status IN ('pending','pending_dispatch')
             OR (status='dispatching' AND next_at<=?))
        AND next_at<=?
      ORDER BY created""",
   (now,now)).fetchall()]

def claim_confirmation_dispatch(db,message_id):
 """Atomically reserve one pending confirmation for one upload worker."""
 init(db); now=time.time()
 with q.conn(db) as c:
  row=c.execute("SELECT status,next_at,worker_pid,source FROM confirmation_receipts WHERE message_id=?",(message_id,)).fetchone()
  if row and row['status']=='dispatching' and row['next_at']<=now and row['worker_pid']:
   try:
    command=subprocess.check_output(['ps','-p',str(row['worker_pid']),'-o','command='],text=True,timeout=2).strip()
    alive=f"production-confirm:{row['source']}:{message_id}" in command
   except Exception:
    alive=False
   if alive:
    c.execute("UPDATE confirmation_receipts SET next_at=? WHERE message_id=? AND status='dispatching'",
              (now+60,message_id))
    return False
  if not row or not (row['status'] in ('pending','pending_dispatch') or
                     (row['status']=='dispatching' and row['next_at']<=now)):
   return False
  updated=c.execute("""UPDATE confirmation_receipts
    SET status='dispatching',attempts=attempts+1,next_at=?
    WHERE message_id=? AND
      (status IN ('pending','pending_dispatch') OR
       (status='dispatching' AND next_at<=?))""",
    (now+60,message_id,now)).rowcount
  return bool(updated)

def mark_confirmation_dispatch_started(db,message_id,pid):
 init(db); now=time.time()
 with q.conn(db) as c:
  c.execute("""UPDATE confirmation_receipts
    SET worker_pid=?,next_at=?,dispatch_started_at=COALESCE(dispatch_started_at,?),
        dispatched_at=NULL,dispatch_duration_ms=NULL
    WHERE message_id=? AND status='dispatching'""",(int(pid),now+60,now,message_id))

def mark_confirmation_dispatch_failed(db,message_id,error):
 """Return a failed worker handoff to the short retry queue."""
 init(db); now=time.time()
 with q.conn(db) as c:
  row=c.execute("SELECT source,status,attempts FROM confirmation_receipts WHERE message_id=?",
                (message_id,)).fetchone()
  # A deterministic worker can commit and verify GitHub successfully, then
  # fail only while sending the Feishu receipt. Keep that upload authoritative
  # and queue only the receipt for retry; never spend upload attempts or block
  # an already-committed batch.
  if row and _verified_upload_receipt(db, row['source'], message_id):
   c.execute("""UPDATE confirmation_receipts
     SET status='reply_pending',reply_attempts=reply_attempts+1,error=?,next_at=?,worker_pid=NULL
     WHERE message_id=? AND status='dispatching'""",
     (str(error)[:240], now + 30, message_id))
   return
  # Claim increments attempts before the worker starts. Do not increment
  # again on error/exit; a late duplicate failure must be idempotent. A
  # failed upload stays retryable forever with bounded exponential backoff;
  # only a verified receipt can finish the batch.
  attempts = int(row['attempts'] or 0) if row else 0
  delay = _dispatch_retry_delay(attempts) if attempts else DISPATCH_RETRY_BASE_SECONDS
  c.execute("""UPDATE confirmation_receipts
    SET status='pending_dispatch', error=?, error_detail=?, next_at=?, worker_pid=NULL
    WHERE message_id=? AND status='dispatching'""",
    (str(error)[:240], str(error), now + delay, message_id))
  # Preserve a pre-claim failure for diagnosis without inventing a worker
  # attempt or consuming the three-attempt upload budget.
  if row and row['status'] in ('pending','pending_dispatch'):
   c.execute("UPDATE confirmation_receipts SET error=?,error_detail=? WHERE message_id=? AND status IN ('pending','pending_dispatch')",
             (str(error)[:240],str(error),message_id))

def mark_confirmation_blocked(db,message_id,error):
 """Keep a preflight failure retryable while retaining its full diagnosis."""
 init(db); now=time.time()
 with q.conn(db) as c:
  c.execute("""UPDATE confirmation_receipts
    SET status='pending_dispatch',error=?,error_detail=?,next_at=?,worker_pid=NULL
    WHERE message_id=? AND status IN ('pending','pending_dispatch','dispatching')""",
    (str(error)[:240],str(error),now+DISPATCH_RETRY_BASE_SECONDS,message_id))

def requeue_blocked_confirmation(db, message_id):
 """Requeue one known false-positive duplicate guard failure.

 A deterministic preflight may have seen a date heading whose body only
 contained the ledger's ``暂无有效记录`` placeholder.  After the guard is
 repaired, the original employee confirmation remains authoritative and can
 be resumed without asking the employee to confirm again.  Keep this command
 deliberately narrow: it only accepts a blocked receipt carrying the old
 duplicate-backfill error, and resets the bounded dispatch attempt counter.
 """
 init(db); now=time.time()
 with q.conn(db) as c:
  row=c.execute("SELECT * FROM confirmation_receipts WHERE message_id=?",(message_id,)).fetchone()
  if not row:
   raise ValueError('没有找到确认回执')
  if row['status']!='blocked':
   raise ValueError(f"确认回执当前状态为 {row['status']}，无需重新排队")
  error=row['error'] or ''
  if '已有有效记录，禁止重复补报' not in error:
   raise ValueError('该阻断不是已知的补报占位误判，拒绝自动恢复')
  note='已修复补报日期占位误判，原确认回执重新排队'
  c.execute("""UPDATE confirmation_receipts
    SET status='pending_dispatch', error=?, attempts=0, next_at=?, worker_pid=NULL
    WHERE message_id=? AND status='blocked'""", (note,now,message_id))
 return {'message_id':message_id,'status':'pending_dispatch','attempts_reset':True}

def quarantine_confirmation(db,message_id,error):
 """Stop retrying a confirmation that has no source binding."""
 init(db); now=time.time()
 with q.conn(db) as c:
  c.execute("""UPDATE confirmation_receipts
    SET status='unmatched',error=?,next_at=?,worker_pid=NULL
    WHERE message_id=? AND source IS NULL""",
    (str(error)[:240],now+86400,message_id))

def verify_dispatch_receipt(db,source,confirmation):
 """Require commit_guard's verified remote-readback receipt before success."""
 import hashlib
 path=Path(db).parent/'receipts'/(hashlib.sha256(source.encode()).hexdigest()+'.json')
 try:
  receipt=json.loads(path.read_text())
 except Exception as exc:
  raise ValueError('没有找到已验证的上传回执') from exc
 if receipt.get('status')=='committed_pending_reread' and str(Path(db).resolve())==str(Path.home().joinpath('.openclaw/state/production-parallel/inbox.sqlite').resolve()):
  from commit_guard import recover_pending_receipt
  receipt=recover_pending_receipt(source,confirmation)
 if (receipt.get('status')!='verified' or receipt.get('source')!=source or
     receipt.get('confirmation')!=confirmation or not receipt.get('commit')):
  raise ValueError('上传回执尚未完成远程回读核验')
 # Older verified receipts predate the retry-to-next-date hook and do not
 # carry the structured report needed by the advancement callback. Derive it
 # from the already persisted source row so recovery remains idempotent.
 if not receipt.get('report'):
  try:
   from commit_guard import report_from_inbox_row
   source_row=q.get(db,source)
   if source_row:
    receipt['report']=report_from_inbox_row(source_row)
  except Exception:
   pass
 return receipt

def replay_confirmation(db,receipt):
 """Bind one durable receipt; repeated replay is idempotent."""
 try:
  result=act_text(db,receipt['text'],receipt['sender'],receipt['grp'],receipt['message_id'])
 except Exception as exc:
  init(db); now=time.time()
  with q.conn(db) as c:
   c.execute("""UPDATE confirmation_receipts
     SET error=?, attempts=attempts+1, next_at=? WHERE message_id=?""",
    (type(exc).__name__+': '+str(exc)[:200],now+30,receipt['message_id']))
  raise
 with q.conn(db) as c:
  c.execute("UPDATE confirmation_receipts SET status='pending_dispatch',error=NULL,next_at=? WHERE message_id=?",
            (time.time(),receipt['message_id']))
 return result

def replay_confirmation_by_id(db,message_id):
 init(db)
 with q.conn(db) as c:
  receipt=c.execute("""SELECT * FROM confirmation_receipts
    WHERE message_id=? AND
      (status IN ('pending','pending_dispatch') OR
       (status='dispatching' AND next_at<=?))""", (message_id,time.time())).fetchone()
 if not receipt: raise ValueError('没有待重试的确认回执')
 with q.conn(db) as c:
  canonical=c.execute("""SELECT message_id FROM confirmation_receipts
    WHERE source=? AND status IN ('pending','pending_dispatch','dispatching','dispatched')
    ORDER BY created ASC LIMIT 1""", (receipt['source'],)).fetchone()
 if canonical and canonical['message_id'] != message_id:
  return {'source':receipt['source'],'token':receipt['token'],'action':'confirm',
          'callback':canonical['message_id'],'retrigger':True,
          'already_dispatched':False,'already_processing':True}
 if receipt['status'] in ('pending_dispatch','dispatching') or (receipt['status']=='pending' and receipt['source'] and receipt['token']):
  return {'source':receipt['source'],'token':receipt['token'],'action':'confirm',
          'callback':receipt['message_id'],'retrigger':True}
 return replay_confirmation(db,dict(receipt))

def mark_confirmation_dispatched(db,message_id,reply_message_id=None):
 init(db)
 now=time.time()
 with q.conn(db) as c:
  row=c.execute("SELECT source FROM confirmation_receipts WHERE message_id=?",(message_id,)).fetchone()
  if not row or not row['source']:
   raise ValueError('确认回执没有绑定原始报数')
  verify_dispatch_receipt(db,row['source'],message_id)
  c.execute("""UPDATE confirmation_receipts
    SET status='dispatched',error=NULL,error_detail=NULL,worker_pid=NULL,
        reply_message_id=COALESCE(?,reply_message_id),dispatched_at=?,
        dispatch_duration_ms=CASE WHEN dispatch_started_at IS NOT NULL
          THEN (? - dispatch_started_at) * 1000
          ELSE (? - created) * 1000 END
    WHERE message_id=? AND
      (status IN ('pending','pending_dispatch','dispatching','reply_pending','reply_blocked','blocked') OR
       (status='dispatched' AND ? IS NOT NULL))""",
            (reply_message_id,now,now,now,message_id,reply_message_id))

def retry_verified_replies(db, now=None):
 """Retry only Feishu receipts for batches already verified on GitHub."""
 init(db); now=time.time() if now is None else now
 with q.conn(db) as c:
  rows=[dict(r) for r in c.execute(
   "SELECT * FROM confirmation_receipts WHERE status='reply_pending' AND next_at<=? ORDER BY created",
   (now,)).fetchall()]
 results=[]
 send_script=Path(__file__).resolve().parent.parent/'group-companion'/'send_upload_receipt.py'
 for row in rows:
  try:
   receipt=verify_dispatch_receipt(db,row['source'],row['message_id'])
   proc=subprocess.run([sys.executable,str(send_script),'--group',row['grp'],
                        '--receipt-json',json.dumps(receipt,ensure_ascii=False)],
                       text=True,capture_output=True,timeout=15)
   if proc.returncode:
    raise RuntimeError((proc.stderr or proc.stdout or '飞书回执发送失败').strip()[-240:])
   payload=json.loads((proc.stdout or '').strip().splitlines()[-1])
   mark_confirmation_dispatched(db,row['message_id'],payload.get('message_id'))
   results.append({'message_id':row['message_id'],'action':'reply_dispatched'})
  except Exception as exc:
   with q.conn(db) as c:
    c.execute("""UPDATE confirmation_receipts
      SET status=CASE WHEN reply_attempts+1>=? THEN 'reply_blocked' ELSE 'reply_pending' END,
          reply_attempts=reply_attempts+1,
          error=?,next_at=CASE WHEN reply_attempts+1>=? THEN ? ELSE ? END,worker_pid=NULL
      WHERE message_id=? AND status='reply_pending'""",
      (MAX_REPLY_ATTEMPTS, type(exc).__name__+': '+str(exc)[:200], MAX_REPLY_ATTEMPTS,
       time.time()+86400,time.time()+30,row['message_id']))
   results.append({'message_id':row['message_id'],'action':'reply_retry',
                   'error':type(exc).__name__+': '+str(exc)[:200]})
 return results
def issue(db,source,summary):
 init(db);a=q.get(db,source)
 if not a or a['grp']!=GROUP:raise ValueError('找不到本群报数')
 if _verified_upload_receipt(db, source):raise ValueError('该批次已上传，禁止重复生成核对卡')
 if not ready_report(a):raise ValueError('原报数尚未完成结构化核对，不能发确认卡')
 from worker_identity import lookup
 identity=lookup(db,a['sender'])
 extracted_result=a.get('result') or {}
 extracted=extracted_result.get('extracted') or {}
 status_only=bool(extracted.get('status_only') or extracted.get('attendance_status'))
 status_type=extracted.get('attendance_status') or ('not_worked' if extracted.get('not_worked') else None)
 proxy=bool(extracted.get('proxy')) and a['sender']==OWNER and extracted.get('worker') in ('A','B','C','D')
 if not identity and not proxy:
  raise ValueError('发送账号尚未绑定员工代号，不能生成可上传核对单')
 if proxy:
  identity={'worker':extracted['worker'],'name':{'A':'徐超超','B':'梅芳','C':'李鸿玉','D':'张小翠'}[extracted['worker']]}
 if (not status_only and (not summary.strip() or
     not all(x in summary for x in ['棉堆堆袜','冰冰袜','小腿袜','过膝袜','女船袜','男船袜']) or
     '合计' not in summary or
     not re.search(r'：\s*\d+', summary))):
  raise ValueError('核对清单必须包含六个产品、合计和至少一项明确数量（0 也有效）')
 identity_line=(f"老板代报：{identity['worker']}={identity['name']} 请核实"
                if proxy else f"身份：{identity['worker']}={identity['name']} 请核实")
 if re.search(r'(?m)^(?:员工代号|身份|老板代报)：.*$',summary):
  summary=re.sub(r'(?m)^(?:员工代号|身份|老板代报)：.*$',identity_line,summary,count=1)
 else:
  summary=identity_line+'\n'+summary
 if not status_only:
  summary=card_builder.normalize_summary(summary)
  summary=re.sub(r'(?m)^([^\n：]+)：0(?:（[^\n]*）)?$',r'\1：0双（数量为0，请核实）',summary)
 token=secrets.token_hex(6);expires=time.time()+86400
 with q.conn(db) as c:
  c.execute('BEGIN IMMEDIATE');c.execute("UPDATE review_cards SET state='superseded' WHERE source=? AND state IN ('pending','confirmed')",(source,))
  c.execute('''INSERT INTO review_cards(
    token,source,sender,grp,summary,digest,expires,state,callback,result,
    delivery_next_at,backfill,card_kind,status_type)
    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
    (token,source,a['sender'],a['grp'],summary,a['digest'],expires,'pending',
     None,None,time.time(),1 if extracted.get('backfill') else 0,
     'status' if status_only else 'production',status_type))
 card=(card_builder.status_payload(summary,status=status_type or 'not_worked')
       if status_only else card_builder.payload(summary,backfill=bool(extracted.get('backfill'))))
 return {'token':token,'source':source,'card':card}

def card_payload(row):
 """Rebuild the exact original card for a retry using the same idempotency key."""
 data=dict(row) if not isinstance(row,dict) else row
 if data.get('card_kind') == 'status':
  return card_builder.status_payload(data['summary'], status=data.get('status_type') or 'not_worked')
 backfill=bool(data.get('backfill',0))
 return card_builder.payload(data['summary'],backfill=backfill)


def deliver_card(db, token, is_resend=False):
 """Send one card and persist a receipt; never create a second token/batch."""
 init(db)
 with q.conn(db) as c:
  row=c.execute('SELECT * FROM review_cards WHERE token=?',(token,)).fetchone()
 if not row or row['state']!='pending' or row['expires']<=time.time():
  return {'token':token,'status':'skipped'}
 row=dict(row)
 try:
  import sys
  transport_dir=Path(__file__).resolve().parent.parent/'group-companion'
  if str(transport_dir) not in sys.path: sys.path.insert(0,str(transport_dir))
  from transport import request
  response=request('xiaowen','POST','/im/v1/messages?receive_id_type=chat_id',
                   {'receive_id':row['grp'],'msg_type':'interactive',
                    'content':json.dumps(card_payload(row),ensure_ascii=False),
                    'uuid':row['token']})
  message_id=((response.get('data') or {}).get('message_id') if isinstance(response,dict) else None)
  if not message_id: raise RuntimeError('飞书未返回消息ID')
  # Feishu's "export messages to document" view cannot serialize interactive
  # cards and renders them as [卡片消息].  Status cards are read-only reports,
  # so also emit a plain-text mirror that remains searchable and printable.
  if row.get('card_kind') == 'status':
   request('xiaowen','POST','/im/v1/messages?receive_id_type=chat_id',
           {'receive_id':row['grp'],'msg_type':'text',
            'content':json.dumps({'text':row['summary']},ensure_ascii=False),
            'uuid':row['token']+'-printable'})
 except urllib.error.HTTPError as exc:
  detail=f'Feishu HTTP {exc.code}: {exc.reason}'
  mark_card_delivery_failed(db, token, detail)
  return {'token':token,'status':'failed','error':detail}
 except RuntimeError as exc:
  mark_card_delivery_failed(db, token, str(exc))
  return {'token':token,'status':'failed','error':str(exc)}
 except Exception as exc:
  mark_card_delivery_unknown(db, token, type(exc).__name__+': '+str(exc))
  return {'token':token,'status':'unknown','error':type(exc).__name__+': '+str(exc)}
 mark_card_delivery_sent(db, token, message_id, is_resend=is_resend)
 return {'token':token,'status':'sent','messageId':message_id,'resend':is_resend}

def scan_card_delivery(db, resend=False):
 """Process due delivery work; callers run this at the 30-second boundary."""
 rows=pending_card_resends(db) if resend else pending_card_deliveries(db)
 results=[]
 for row in rows:
  results.append(deliver_card(db,row['token'],is_resend=resend or bool(row.get('last_delivery_at'))))
 return results
def act(db,token,action,sender,group,callback):
 init(db)
 if not callback.startswith('card-action-'):raise ValueError('必须从本人核对卡片操作')
 if action not in ('confirm','modify','defer'):raise ValueError('未知操作')
 from commit_guard import harmless_chat
 with q.conn(db) as c:
  c.execute('BEGIN IMMEDIATE');r=c.execute('SELECT * FROM review_cards WHERE token=?',(token,)).fetchone()
  if not r or r['sender']!=sender or r['grp']!=group:raise ValueError('这张核对卡片不属于你或当前群')
  if _verified_upload_receipt(db, r['source']):raise ValueError('该批次已上传，旧核对卡已停用')
  if r['callback']==callback and r['result']:return json.loads(r['result'])
  if r['expires']<time.time() or r['state']!='pending':raise ValueError('卡片已过期或失效，请重新核对')
  blocked=c.execute("""SELECT token FROM confirmation_receipts
    WHERE source=? AND status='blocked' ORDER BY created DESC LIMIT 1""", (r['source'],)).fetchone()
  if blocked and (not blocked['token'] or blocked['token']==r['token']):
   raise ValueError('该批次上传已暂停，需修复后重新生成核对卡')
  a=c.execute('SELECT * FROM inbox WHERE id=?',(r['source'],)).fetchone()
  if not a or a['digest']!=r['digest']:raise ValueError('原报数已改变')
  source_record=dict(a);source_record['result']=json.loads(source_record['result']) if source_record['result'] else None
  if not ready_report(source_record):raise ValueError('原报数尚未完成结构化核对，不能确认')
  if action=='confirm':
   existing=c.execute("""SELECT message_id,status FROM confirmation_receipts
     WHERE source=? AND status IN ('pending','pending_dispatch','dispatching','dispatched')
     ORDER BY created DESC LIMIT 1""",(r['source'],)).fetchone()
   if existing:
    result={'source':r['source'],'token':token,'action':'confirm','callback':existing['message_id'],
            'already_dispatched':existing['status']=='dispatched',
            'already_processing':existing['status']!='dispatched'}
    # A card can be reissued after the employee already confirmed the source.
    # Carry that durable confirmation onto the newest card so it cannot ask
    # the employee to confirm the same batch again.
    c.execute("""UPDATE review_cards SET state='confirmed',callback=?,result=?,
      confirmation_retry_status='bound',confirmation_message_id=?,confirmation_error=NULL
      WHERE token=? AND state='pending'""",
      (existing['message_id'],json.dumps(result),existing['message_id'],token))
    return result
   for row in c.execute('SELECT event,status,result FROM inbox WHERE sender=? AND grp=? AND created>? AND id<>?',(sender,group,a['created'],callback)):
    if not harmless_chat(dict(row)):raise ValueError('期间有更正或待判断消息，请重新生成核对卡片')
  result={'source':r['source'],'token':token,'action':action,'callback':callback}
  c.execute("""UPDATE review_cards SET state=?,callback=?,result=?,
    confirmation_retry_status=CASE WHEN ?='confirm' THEN 'bound' ELSE confirmation_retry_status END,
    confirmation_message_id=CASE WHEN ?='confirm' THEN ? ELSE confirmation_message_id END,
    confirmation_error=NULL WHERE token=?""",
    ({'confirm':'confirmed','modify':'modified','defer':'deferred'}[action],callback,
     json.dumps(result),action,action,callback,token))
  if action=='confirm':
   _queue_confirmation_receipt(c,callback,sender,group,'准确',r['source'],token)
 return result
def act_text(db,text,sender,group,callback):
 """Bind a direct human confirmation to the sender's newest pending card."""
 if not re.fullmatch(r'(?:准确|确认|确认上传)(?:\s+om_[A-Za-z0-9_-]+)?', text.strip()):
  raise ValueError('不是确认消息')
 init(db)
 from batch_status import latest_actionable_batch
 explicit_source = text.strip().split()[1] if len(text.strip().split()) > 1 else None
 with q.conn(db) as c:
  pending = c.execute("SELECT DISTINCT source FROM review_cards WHERE sender=? AND grp=? AND state='pending' AND expires>?", (sender,group,time.time())).fetchall()
  active_sources = [r['source'] for r in pending if not _verified_upload_receipt(db,r['source'])]
 if not explicit_source and len(active_sources)>1:
  raise ValueError('有多张待核对卡，请在对应卡片上确认，不能用无日期的准确匹配最新一张')
 actionable=latest_actionable_batch(db,sender,group)
 if explicit_source:
  with q.conn(db) as c:
   bound=c.execute("SELECT source,state FROM review_cards WHERE source=? AND sender=? AND grp=? AND state IN ('pending','confirmed') AND expires>? ORDER BY rowid DESC LIMIT 1",(explicit_source,sender,group,time.time())).fetchone()
  if not bound: raise ValueError('指定核对卡不存在或已失效')
  actionable=dict(bound)
 if not actionable:
  # A repeated employee message can arrive after the source has already
  # reached a verified GitHub receipt.  In that state there is deliberately
  # no actionable card, but the gateway must still answer idempotently rather
  # than recording a new failed confirmation that could re-enter dispatch.
  with q.conn(db) as c:
   completed=c.execute("""SELECT token,source FROM review_cards
     WHERE sender=? AND grp=? AND state IN ('pending','confirmed') AND expires>?
     ORDER BY rowid DESC""",(sender,group,time.time())).fetchall()
   for card in completed:
    if not _verified_upload_receipt(db,card['source']):
     continue
    receipt=c.execute("""SELECT message_id,status FROM confirmation_receipts
      WHERE source=? AND status='dispatched' ORDER BY created ASC LIMIT 1""",(card['source'],)).fetchone()
    if receipt:
     return {'source':card['source'],'token':card['token'],'action':'confirm',
             'callback':receipt['message_id'],'already_dispatched':True,
             'already_processing':False}
  raise ValueError('没有本人待确认核对卡')
 if actionable['state']=='needs_reconciliation':
  # If this source already has a durable confirmation receipt, a reissued
  # card's uncertain delivery must not make the employee confirm again. The
  # receipt is the authoritative hand-off; bind the card below and let the
  # existing recovery queue finish the upload.
  with q.conn(db) as c:
   durable=c.execute("""SELECT 1 FROM confirmation_receipts
     WHERE source=? AND status IN ('pending','pending_dispatch','dispatching','dispatched')
     LIMIT 1""",(actionable['source'],)).fetchone()
  if not durable:
   raise ValueError('当前核对卡送达状态不明确，请先核实后再确认')
 from commit_guard import blocking_message
 with q.conn(db) as c:
  r=None
  candidate=c.execute("SELECT * FROM review_cards WHERE source=? AND sender=? AND grp=? AND state='pending' AND expires>? ORDER BY rowid DESC LIMIT 1",(actionable['source'],sender,group,time.time())).fetchone()
  if candidate:
   r=candidate
  retrigger = False
  if not r:
   # A previous confirmation can be durable while the upload worker or the
   # network failed afterwards. Allow the same employee to retry the exact
   # confirmed source, but never search another employee's card.
   candidate=c.execute("SELECT * FROM review_cards WHERE source=? AND sender=? AND grp=? AND state='confirmed' AND expires>? ORDER BY rowid DESC LIMIT 1",(actionable['source'],sender,group,time.time())).fetchone()
  if candidate:
   r=candidate
  retrigger = bool(r)
  if not r: raise ValueError('没有本人待确认核对卡')
  blocked=c.execute("""SELECT token FROM confirmation_receipts
    WHERE source=? AND status='blocked' ORDER BY created DESC LIMIT 1""", (r['source'],)).fetchone()
  if blocked and (not blocked['token'] or blocked['token']==r['token']):
   raise ValueError('该批次上传已暂停，需修复后重新生成核对卡')
  if _verified_upload_receipt(db, r['source']):raise ValueError('该批次已上传，旧核对卡已停用')
  if r['callback']==callback and r['result']:
   # The same Feishu message can be delivered more than once after a
   # reconnect. Return the durable upload state so the gateway does not
   # start a second worker or send a second progress reply.
   result=json.loads(r['result'])
   existing=c.execute("""SELECT status FROM confirmation_receipts
     WHERE source=? ORDER BY created DESC LIMIT 1""",(r['source'],)).fetchone()
   if existing:
    result['already_dispatched']=existing['status']=='dispatched'
    result['already_processing']=existing['status'] in ('pending','pending_dispatch','dispatching')
   return result
  existing=c.execute("""SELECT message_id,status FROM confirmation_receipts
    WHERE source=? AND status IN ('pending','pending_dispatch','dispatching','dispatched')
    ORDER BY created DESC LIMIT 1""",(r['source'],)).fetchone()
  if existing:
   result={'source':r['source'],'token':r['token'],'action':'confirm',
           'callback':existing['message_id'],'retrigger':True,
           'already_dispatched':existing['status']=='dispatched',
           'already_processing':existing['status']!='dispatched'}
   # The newest card may have superseded the card that owns the durable
   # confirmation receipt. Bind the receipt to this card as well, without
   # creating another receipt or upload task.
   c.execute("""UPDATE review_cards SET state='confirmed',callback=?,result=?,
     confirmation_retry_status='bound',confirmation_text=?,
     confirmation_message_id=?,confirmation_error=NULL
     WHERE token=? AND state='pending'""",
     (existing['message_id'],json.dumps(result),text,existing['message_id'],r['token']))
   return result
  a=c.execute('SELECT * FROM inbox WHERE id=?',(r['source'],)).fetchone()
  if not a or a['digest']!=r['digest']: raise ValueError('原报数已改变')
  source_result=json.loads(a['result']) if a['result'] else {}
  source_date=(source_result.get('extracted') or {}).get('production_date')
  for row in c.execute("SELECT event,status,result FROM inbox WHERE sender=? AND grp=? AND created>? AND created<? AND id<>? ORDER BY created",(sender,group,a['created'],time.time(),callback)).fetchall():
   candidate=dict(row)
   if blocking_message(candidate):
    # A later report for another production day is a separate batch, not a
    # correction to this card. Only same-day reports/corrections can block.
    candidate_result=json.loads(candidate['result']) if candidate.get('result') else {}
    candidate_date=(candidate_result.get('extracted') or {}).get('production_date')
    candidate_kind=(candidate_result.get('extracted') or {}).get('kind')
    if candidate_kind == 'confirmation':
     continue
    if source_date and candidate_date and candidate_date != source_date and candidate_kind in ('report','correction'):
     continue
    raise ValueError('确认前存在更正或补报，请重新核对')
  result={'source':r['source'],'token':r['token'],'action':'confirm','callback':callback,
          'retrigger':retrigger}
  c.execute("""UPDATE review_cards SET state='confirmed',callback=?,result=?,
    confirmation_retry_status='bound', confirmation_text=?,
    confirmation_message_id=?, confirmation_error=NULL WHERE token=?""",
    (callback,json.dumps(result),text,callback,r['token']))
  _queue_confirmation_receipt(c,callback,sender,group,text,r['source'],r['token'])
 return result

def confirmation(db,source,callback):
 init(db)
 with q.conn(db) as c:
  r=c.execute("SELECT * FROM review_cards WHERE source=? AND callback=? AND state='confirmed'",(source,callback)).fetchone()
  if not r:raise ValueError('没有对应的有效卡片确认')
  return dict(r)
def main():
 from service import DB
 p=argparse.ArgumentParser();p.add_argument('command',choices=['send','retry-card-deliveries','resend-unconfirmed-cards','retry-verified-replies','action','record-failed','pending-confirmations','replay-confirmation','requeue-blocked-confirmation','claim-dispatch','mark-dispatched','mark-dispatch-failed','mark-dispatch-blocked','mark-dispatch-started','quarantine-confirmation','verify-dispatch']);p.add_argument('--source');p.add_argument('--summary-file');p.add_argument('--message-id');p.add_argument('--confirmation');p.add_argument('--reply-message-id');p.add_argument('--pid',type=int);p.add_argument('--error');a=p.parse_args()
 if a.command=='action':
  e=json.load(sys.stdin);text=e.get('text','').strip()
  m=re.fullmatch(r'生产核对 (confirm|modify|defer) ([a-f0-9]{12})',text)
  if m: print(json.dumps(act(DB,m[2],m[1],e['sender'],e['group'],e['id']),ensure_ascii=False));return
  print(json.dumps(act_text(DB,text,e['sender'],e['group'],e['id']),ensure_ascii=False));return
 if a.command=='record-failed':
  e=json.load(sys.stdin);record_failed_confirmation(DB,e['text'],e['sender'],e['group'],e['id'],e.get('error','unknown failure'));print(json.dumps({'recorded':True},ensure_ascii=False));return
 if a.command=='pending-confirmations':
  print(json.dumps(pending_confirmations(DB),ensure_ascii=False));return
 if a.command=='replay-confirmation':
  print(json.dumps(replay_confirmation_by_id(DB,a.message_id),ensure_ascii=False));return
 if a.command=='requeue-blocked-confirmation':
  print(json.dumps(requeue_blocked_confirmation(DB,a.message_id),ensure_ascii=False));return
 if a.command=='claim-dispatch':
  print(json.dumps({'claimed':claim_confirmation_dispatch(DB,a.message_id)},ensure_ascii=False));return
 if a.command=='mark-dispatched':
  mark_confirmation_dispatched(DB,a.message_id,a.reply_message_id);print(json.dumps({'marked':True,'replyMessageId':a.reply_message_id},ensure_ascii=False));return
 if a.command=='mark-dispatch-failed':
  mark_confirmation_dispatch_failed(DB,a.message_id,a.error or '上传任务失败');print(json.dumps({'marked':True},ensure_ascii=False));return
 if a.command=='mark-dispatch-blocked':
  mark_confirmation_blocked(DB,a.message_id,a.error or '确认被前置校验拦截');print(json.dumps({'marked':True},ensure_ascii=False));return
 if a.command=='mark-dispatch-started':
  mark_confirmation_dispatch_started(DB,a.message_id,a.pid);print(json.dumps({'marked':True},ensure_ascii=False));return
 if a.command=='quarantine-confirmation':
  quarantine_confirmation(DB,a.message_id,a.error or '确认回执缺少原始报数');print(json.dumps({'marked':True},ensure_ascii=False));return
 if a.command=='verify-dispatch':
  print(json.dumps(verify_dispatch_receipt(DB,a.source,a.confirmation),ensure_ascii=False));return
 if a.command=='retry-card-deliveries':
  print(json.dumps(scan_card_delivery(DB,False),ensure_ascii=False));return
 if a.command=='resend-unconfirmed-cards':
  print(json.dumps(scan_card_delivery(DB,True),ensure_ascii=False));return
 if a.command=='retry-verified-replies':
  print(json.dumps(retry_verified_replies(DB),ensure_ascii=False));return
 r=issue(DB,a.source,Path(a.summary_file).read_text())
 result=deliver_card(DB,r['token'])
 print(json.dumps({'token':r['token'],'source':a.source,**result},ensure_ascii=False))
if __name__=='__main__':main()
