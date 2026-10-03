"""One durable speaker decision shared by both native gateways."""
import json,re,sqlite3,time,os,sys
from contextlib import contextmanager
from pathlib import Path
GROUP='oc_1f8587b1bcde12a0d1bb6053ab2b748a'
STATE=Path.home()/'.openclaw/state/group-companion'
DB=STATE/'routing.sqlite'
WORK=re.compile(r'(棉堆堆袜|冰冰袜|小腿袜|过膝袜|女船袜|男船袜|下机|烤边|拷边|报数|产量|数量|上传|台账|更正|撤销|批次|准确|确认|继续|重试|恢复上传|远程仓库|仓库|保存到|打通|核实)')
SOCIAL=re.compile(r'(你好|您好|大家.*好|早上好|晚上好|早安|晚安|辛苦|好累|真累|太累|累了|累死|哈哈|笑话|开心|天气|吃饭|下班|节日|快乐|聊聊|[?？])')
IDENTITY_NOTICE=re.compile(r'^\s*(?:[ABCD]\s*[=:：]\s*(?:徐超超|梅芳|李鸿玉|张小翠)|(?:徐超超|梅芳|李鸿玉|张小翠)\s*[=:：]\s*[ABCD])\s*$')
@contextmanager
def connect(db):
 c=sqlite3.connect(db,timeout=8)
 try:
  yield c
  c.commit()
 except Exception:
  c.rollback()
  raise
 finally:
  c.close()
def decide(db,message,now=None,force=False):
 now=time.time() if now is None else now
 Path(db).parent.mkdir(parents=True,exist_ok=True)
 with connect(db) as c:
  c.execute('CREATE TABLE IF NOT EXISTS routes(id TEXT PRIMARY KEY,owner TEXT,reason TEXT,created REAL)')
  c.execute('CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value REAL)')
  c.execute('CREATE TABLE IF NOT EXISTS threads(sender TEXT PRIMARY KEY,owner TEXT,updated REAL)')
  c.execute('BEGIN IMMEDIATE')
  old=c.execute('SELECT owner,reason FROM routes WHERE id=?',(message['id'],)).fetchone()
  if old and not force:return dict(owner=old[0],reason=old[1])
  text=message.get('text','');mentions=message.get('mentions',[])
  named=set(x for x in mentions if x in ('xiaowen','yuanbao'))
  if '小文' in text:named.add('xiaowen')
  if '元宝' in text:named.add('yuanbao')
  reply=message.get('reply_owner')
  sticky=c.execute('SELECT owner,updated FROM threads WHERE sender=?',(message.get('sender',''),)).fetchone()
  quiet=c.execute("SELECT value FROM meta WHERE key='quiet_until'").fetchone()
  control=text.strip().strip('。！!')
  owner,reason='none','human_conversation'
  if message.get('routing_failure'):owner,reason='xiaowen','canonical_unavailable'
  elif message.get('is_bot'):reason='ignore_bot'
  elif control in ('先安静一会儿','安静一会儿','恢复聊天','活跃一点'):
   c.execute("INSERT OR REPLACE INTO meta VALUES('quiet_until',?)",(now+1800 if '安静' in control else 0,))
   owner,reason='xiaowen','chat_control'
  elif IDENTITY_NOTICE.fullmatch(control):owner,reason='xiaowen','work_review'
  elif WORK.search(text) or re.search(r'\d+\s*(双|打|件)',text) or (re.search(r'\d',text) and re.fullmatch(r'[\d\s，,。：:.、;；-]+',text)) or message.get('media'):owner,reason='xiaowen','work_review'
  elif len(named)==1:owner,reason=next(iter(named)),'named'
  elif reply in ('xiaowen','yuanbao') and not named:owner,reason=reply,'reply_to_bot'
  elif (mentions and not named) or (reply=='human' and not named):pass
  elif quiet and quiet[0]>now:reason='quiet_mode'
  elif sticky and now-sticky[1]<=600 and not named:
   if control not in ('谢谢','谢谢你','好的','收到','哈哈','嗯','嗯嗯'):owner,reason=sticky[0],'sender_followup'
  elif named or SOCIAL.search(text):
   last=c.execute("SELECT value FROM meta WHERE key='last_social'").fetchone()
   if named or not last or now-last[0]>=60:
    n=c.execute("SELECT value FROM meta WHERE key='turn'").fetchone();n=int(n[0]) if n else 0
    owner,reason=('xiaowen' if n%2==0 else 'yuanbao'),'social_turn'
    c.execute("INSERT OR REPLACE INTO meta VALUES('turn',?)",(n+1,))
    c.execute("INSERT OR REPLACE INTO meta VALUES('last_social',?)",(now,))
   else:reason='social_cooldown'
  elif message.get('sender'):
   last=c.execute('SELECT owner,updated FROM threads WHERE sender=?',(message['sender'],)).fetchone()
   if last and now-last[1]<=600:owner,reason=last[0],'sender_followup'
  if owner in ('xiaowen','yuanbao') and reason!='work_review' and message.get('sender'):
   c.execute('INSERT OR REPLACE INTO threads VALUES(?,?,?)',(message['sender'],owner,now))
  c.execute('INSERT OR REPLACE INTO routes VALUES(?,?,?,?)',(message['id'],owner,reason,now))
  return dict(owner=owner,reason=reason)

def canonical(message_id):
 from transport import request
 identities=json.loads((STATE/'identities.json').read_text())
 payload=request('xiaowen','GET','/im/v1/messages/'+message_id)
 item=payload['data']['items'][0]
 if item.get('chat_id')!=GROUP:raise ValueError('wrong group')
 sender=item.get('sender',{});sender_id=sender.get('id','')
 text=json.loads(item.get('body',{}).get('content','{}')).get('text','')
 mentions=[]
 for m in item.get('mentions',[]):
  who=next((k for k,v in identities.items() if m.get('id') in v),'human')
  mentions.append(who);text=text.replace(m.get('key','@_unknown'),m.get('name',''))
 parent=item.get('parent_id');reply=None
 if parent:
  try:
   p=request('xiaowen','GET','/im/v1/messages/'+parent)['data']['items'][0]
   reply=next((k for k,v in identities.items() if p.get('sender',{}).get('id') in v),'human')
  except Exception:reply='human'
 return dict(id=message_id,sender=sender_id,text=text,mentions=mentions,reply_owner=reply,
  is_bot=sender.get('sender_type') in ('app','bot') or any(sender_id in v for v in identities.values()),
  media=item.get('msg_type') not in ('text',))

def route_event(event):
 if event.get('group')!=GROUP:return {'owner':'outside','reason':'outside_group'}
 mid=event.get('id','')
 if not re.fullmatch(r'om_[A-Za-z0-9_-]+',mid):return {'owner':'xiaowen','reason':'missing_message_identity'}
 try:
  if DB.exists():
   with connect(DB) as c:
    exists=c.execute("SELECT name FROM sqlite_master WHERE name='routes'").fetchone()
    r=c.execute('SELECT owner,reason FROM routes WHERE id=?',(mid,)).fetchone() if exists else None
    if r:
     result=dict(owner=r[0],reason=r[1])
     # Older versions permanently recorded ordinary greetings and
     # continuation messages as silent human conversation. Re-evaluate those
     # cached decisions so a deployment change can take effect without
     # requiring the user to send the message a second time.
     if result['owner']=='none' and result['reason']=='human_conversation':
      try:
       message=canonical(mid)
       if WORK.search(message.get('text','')) or SOCIAL.search(message.get('text','')):
        result=decide(DB,message,force=True)
      except Exception:
       pass
     direct_xiaowen = result['owner']=='xiaowen' and result['reason']=='named'
     if result['reason'] not in ('work_review','canonical_unavailable') and result['owner']!='none' and not direct_xiaowen:
      from chat_jobs import enqueue,DB as CHAT_DB,connect as connect_chat
      with connect_chat(CHAT_DB) as jobs:exists=jobs.execute('SELECT 1 FROM jobs WHERE id=?',(mid,)).fetchone()
      if not exists:enqueue(CHAT_DB,canonical(mid),result['owner'])
      result['delivery']='queue'
     return result
  try:message=canonical(mid)
  except Exception:message={'id':mid,'routing_failure':True}
  result=decide(DB,message)
  # A direct @小文 mention should be answered by the native agent turn.
  # Queueing it here lets the companion hook mark the event handled before
  # the worker has produced a reply, which can leave the user with silence.
  direct_xiaowen = result['owner']=='xiaowen' and result['reason']=='named'
  if result['owner']!='none' and result['reason'] not in ('work_review','canonical_unavailable') and not direct_xiaowen:
   from chat_jobs import enqueue,DB as CHAT_DB
   enqueue(CHAT_DB,message,result['owner'])
   result['delivery']='queue'
  return result
 except Exception as e:
  print('companion routing state unavailable: '+type(e).__name__,file=sys.stderr)
  return {'owner':'none','reason':'routing_state_unavailable'}
if __name__=='__main__':print(json.dumps(route_event(json.load(sys.stdin))))
