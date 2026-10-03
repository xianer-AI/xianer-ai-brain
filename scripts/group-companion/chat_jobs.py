"""Single durable delivery owner for social turns, independent of production uploads."""
import json,sqlite3,time,secrets,uuid,subprocess,sys,concurrent.futures
from contextlib import contextmanager
from pathlib import Path
from router import STATE,GROUP
DB=STATE/'chat.sqlite'
@contextmanager
def connect(db):
 Path(db).parent.mkdir(parents=True,exist_ok=True)
 c=sqlite3.connect(db,timeout=10);c.row_factory=sqlite3.Row
 try:
  c.execute('CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,sender TEXT,event TEXT,actor TEXT,state TEXT,created REAL,lease TEXT,expires REAL,attempts INTEGER DEFAULT 0,response TEXT,receipt TEXT,error TEXT)')
  yield c
  c.commit()
 except Exception:
  c.rollback()
  raise
 finally:
  c.close()
def enqueue(db,event,actor):
 with connect(db) as c:c.execute("INSERT OR IGNORE INTO jobs(id,sender,event,actor,state,created) VALUES(?,?,?,?,?,?)",(event['id'],event['sender'],json.dumps(event,ensure_ascii=False),actor,'queued',time.time()))
def claim(db):
 with connect(db) as c:
  c.execute('BEGIN IMMEDIATE');now=time.time()
  # A worker can die after marking a job as sending but before Feishu returns.
  # Keep that result uncertain; never put it back in the automatic send queue.
  c.execute("UPDATE jobs SET state='uncertain',lease=NULL,error=COALESCE(error,'send_result_unknown') WHERE state='sending' AND expires<?",(now,))
  c.execute("UPDATE jobs SET state=CASE WHEN attempts<2 THEN 'queued' ELSE 'failed' END WHERE state='processing' AND expires<?",(now,))
  r=c.execute("SELECT * FROM jobs j WHERE state='queued' AND NOT EXISTS(SELECT 1 FROM jobs a WHERE a.sender=j.sender AND a.state IN ('processing','sending')) ORDER BY created,id LIMIT 1").fetchone()
  if not r:return None
  lease=secrets.token_hex(12);c.execute("UPDATE jobs SET state='processing',lease=?,expires=?,attempts=attempts+1 WHERE id=?",(lease,now+240,r['id']))
  return {**dict(r),'lease':lease}
def process(db,row,generate,send):
 try:
  event=json.loads(row['event']);actor=row['actor'];fallback=False
  with connect(db) as c:
   history=[{'user':json.loads(r['event'])['text'],'assistant':r['response'],'actor':r['actor']} for r in c.execute("SELECT event,response,actor FROM jobs WHERE sender=? AND state='sent' AND created>? ORDER BY created DESC LIMIT 6",(row['sender'],time.time()-86400))][::-1]
  try:text=generate(actor,event,history)
  except Exception:
   actor='xiaowen' if actor=='yuanbao' else 'yuanbao';fallback=True;text=generate(actor,event,history)
  text=text.strip()
  if not text or text in ('NO_REPLY','[SILENT]','REPLY_SKIP'):
   with connect(db) as c:c.execute("UPDATE jobs SET state='silent' WHERE id=? AND lease=?",(row['id'],row['lease']))
   return
  if fallback:text=('元宝暂时没接上，我是小文，先来接一下。' if actor=='xiaowen' else '小文暂时忙着，我是元宝，先来接一下。')+'\n'+text
  with connect(db) as c:
   r=c.execute("UPDATE jobs SET state='sending',actor=?,response=? WHERE id=? AND lease=? AND state='processing' AND expires>=?",(actor,text,row['id'],row['lease'],time.time()))
   if r.rowcount!=1:return
  # Persist sending BEFORE network; on uncertain receipt never generate/send a second answer.
  receipt=send(actor,text,str(uuid.uuid5(uuid.NAMESPACE_URL,GROUP+row['id'])))
  with connect(db) as c:c.execute("UPDATE jobs SET state='sent',receipt=? WHERE id=? AND lease=?",(receipt,row['id'],row['lease']))
 except Exception as e:
  with connect(db) as c:c.execute("UPDATE jobs SET state=CASE WHEN state='sending' THEN 'uncertain' ELSE 'failed' END,error=? WHERE id=? AND lease=?",(type(e).__name__,row['id'],row['lease']))
def generate(actor,event,history):
 p=subprocess.run(['/Users/xianer/.hermes/hermes-agent/venv/bin/python',str(Path(__file__).with_name('chat_generate.py')),actor],input=json.dumps({'event':event,'history':history},ensure_ascii=False),capture_output=True,text=True,timeout=145)
 if p.returncode:raise RuntimeError('chat model failed')
 return json.loads(p.stdout)['text']
def send(actor,text,key):
 from transport import request
 # Same-group messages only; display actor's actual bot identity.
 p=request(actor,'POST','/im/v1/messages?receive_id_type=chat_id',{'receive_id':GROUP,'msg_type':'text','content':json.dumps({'text':text},ensure_ascii=False),'uuid':key})
 return p['data']['message_id']
def serve():
 with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
  active=set()
  while True:
   active={f for f in active if not f.done()}
   while len(active)<4:
    row=claim(DB)
    if not row:break
    active.add(pool.submit(process,DB,row,generate,send))
   time.sleep(.5)
if __name__=='__main__':serve()
