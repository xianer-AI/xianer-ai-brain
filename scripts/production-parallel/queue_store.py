"""Durable draft inbox. It deliberately has no ledger write or chat send capability."""
import json,sqlite3,time,uuid,hashlib
from contextlib import contextmanager
from pathlib import Path

@contextmanager
def conn(db):
 Path(db).parent.mkdir(parents=True,exist_ok=True,mode=0o700)
 c=sqlite3.connect(db,timeout=20);c.row_factory=sqlite3.Row
 try:
  with c: yield c
 finally: c.close()

def init(db):
 with conn(db) as c:
  c.execute('PRAGMA journal_mode=WAL')
  c.execute('CREATE TABLE IF NOT EXISTS blocked_message_fingerprints(id_hash TEXT PRIMARY KEY)')
  c.execute('''CREATE TABLE IF NOT EXISTS inbox(id TEXT PRIMARY KEY, sender TEXT NOT NULL, grp TEXT NOT NULL, event TEXT NOT NULL, digest TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued', lease TEXT, expires REAL, result TEXT, attempts INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL, error TEXT)''')

def block_replay(db, message_ids):
 """Retain only one-way ID fingerprints, without message/card content."""
 init(db)
 with conn(db) as c:
  c.executemany('INSERT OR IGNORE INTO blocked_message_fingerprints(id_hash) VALUES(?)',
                [(hashlib.sha256(str(mid).encode()).hexdigest(),) for mid in message_ids])

def put(db,event):
 for k in ['messageId','senderId','groupId','content']:
  if not isinstance(event.get(k),str) or not event[k].strip(): raise ValueError('missing '+k)
 init(db); raw=json.dumps(event,ensure_ascii=False,sort_keys=True);digest=hashlib.sha256(json.dumps({k:event[k] for k in ['messageId','senderId','groupId','content']},sort_keys=True).encode()).hexdigest()
 with conn(db) as c:
  c.execute('BEGIN IMMEDIATE')
  if c.execute('SELECT 1 FROM blocked_message_fingerprints WHERE id_hash=?',(hashlib.sha256(event['messageId'].encode()).hexdigest(),)).fetchone():
   return None
  old=c.execute('SELECT digest FROM inbox WHERE id=?',(event['messageId'],)).fetchone()
  if old:
   if old['digest']!=digest: raise ValueError('same source ID changed; preserve original and request explicit correction')
   return event['messageId']
  c.execute('INSERT INTO inbox(id,sender,grp,event,digest,created) VALUES(?,?,?,?,?,?)',(event['messageId'],event['senderId'],event['groupId'],raw,digest,time.time()))
 return event['messageId']

def claim(db):
 init(db)
 with conn(db) as c:
  c.execute('BEGIN IMMEDIATE');now=time.time()
  c.execute("UPDATE inbox SET status=CASE WHEN attempts>=3 THEN 'failed' ELSE 'queued' END,lease=NULL WHERE status='processing' AND expires<?",(now,))
  row=c.execute("""SELECT * FROM inbox j WHERE j.status='queued' AND NOT EXISTS(SELECT 1 FROM inbox active WHERE active.grp=j.grp AND active.sender=j.sender AND active.status='processing') ORDER BY created,id LIMIT 1""").fetchone()
  if not row:return None
  lease=uuid.uuid4().hex
  c.execute("UPDATE inbox SET status='processing',lease=?,expires=?,attempts=attempts+1 WHERE id=?",(lease,now+180,row['id']))
  return {**dict(row),'lease':lease}

def finish(db,msg,lease,result,error=None):
 with conn(db) as c:
  r=c.execute("UPDATE inbox SET status=?,result=?,error=?,lease=NULL WHERE id=? AND lease=? AND status='processing' AND expires>=?",('failed' if error else 'ready',json.dumps(result,ensure_ascii=False),error,msg,lease,time.time()))
  if r.rowcount!=1:raise ValueError('stale or invalid lease')

def get(db,msg):
 init(db)
 with conn(db) as c:
  r=c.execute('SELECT * FROM inbox WHERE id=?',(msg,)).fetchone()
  if not r:return None
  d=dict(r);d['event']=json.loads(d['event']);d['result']=json.loads(d['result']) if d['result'] else None;return d

def count(db):
 with conn(db) as c:return c.execute('SELECT COUNT(*) FROM inbox').fetchone()[0]
