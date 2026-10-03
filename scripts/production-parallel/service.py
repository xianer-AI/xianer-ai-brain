import json,sys,subprocess,time,concurrent.futures,os,argparse
import re
from pathlib import Path
import queue_store as q
import recovery_scan
import missing_alerts
from hermes_extract import auto_production_date
HERE=Path(__file__).resolve().parent
DB=os.environ.get('PRODUCTION_INBOX_DB',str(Path.home()/'.openclaw/state/production-parallel/inbox.sqlite'))
PYTHON='/Users/xianer/.hermes/hermes-agent/venv/bin/python'
COMPANION=HERE.parent/'group-companion'
RECOVERY_INTERVAL=30.0
PRODUCTS=('棉堆堆袜','冰冰袜','小腿袜','过膝袜','女船袜','男船袜')
# Common employee shorthand/typos are normalized before the deterministic
# parser runs.  Keep the canonical product names in every stored record and
# review card so a harmless wording variation does not stop a normal report.
PRODUCT_ALIASES={
    '冰袜袜':'冰冰袜',
}
if str(COMPANION) not in sys.path: sys.path.insert(0,str(COMPANION))

def recovery_due(last_run, now, interval=RECOVERY_INTERVAL):
 return last_run is None or now-last_run >= interval

def fast_extract(event):
 """Parse the fixed six-product employee format without starting a model."""
 content=str(event.get('content') or '')
 # Capacity/machine analysis is read-only; numbers followed by 台 are not
 # production quantities. Keep this guard before product parsing.
 if re.search(r'(?:机台|产能|历史产量|平均一天|每款产品)', content) and not re.search(r'(?:报数|补报|上报|更正|纠正)', content):
  return None
 aliases_used=[]
 for alias, canonical in PRODUCT_ALIASES.items():
  if alias in content:
   content=content.replace(alias, canonical)
   aliases_used.append(f'{alias}→{canonical}')
 # Card button callbacks are handled by gate.mjs.  Do not send them through
 # the ordinary report parser, otherwise choices 1/2/3/4 can create a false
 # production card (choice 3 previously became an all-zero report).
 message_id=str(event.get('messageId') or '')
 if message_id.startswith('card-action-') or re.fullmatch(
     r'生产补报\s+[1-3](?:\s+[ABCD]\s+20\d{2}-\d{2}-\d{2})?', content.strip()):
  return {'agent':'规则化回调过滤','draft_only':True,
          'extracted':{'kind':'other','worker':'','production_date':None,
                       'items':[],'missing':[],'notes':['核实卡按钮回调已由网关处理'],'backfill':False}}
 if not any(product in content for product in PRODUCTS):
  return None
 name_codes={'徐超超':'A','梅芳':'B','李鸿玉':'C','张小翠':'D'}
 worker=''
 identity=re.search(r'(?m)^\s*([ABCD])(?:\s*[=:：]|\s*$)', content)
 if identity:
  worker=identity.group(1)
 else:
  for name, code in name_codes.items():
   if name in content: worker=code; break
 if not worker:
  try:
   from worker_identity import lookup
   bound = lookup(DB, str(event.get('senderId') or ''))
   worker = bound.get('worker', '') if bound else ''
  except Exception:
   worker = ''
 if not worker:
  return None
 items=[]
 seen=set()
 for product in PRODUCTS:
  match=re.search(rf'{re.escape(product)}\s*(?:[：:=、,，-]\s*)?([0-9][0-9,]*)\s*(?:双)?', content)
  if match and product not in seen:
   items.append({'product':product,'process':'烤边' if worker in ('C','D') else '下机',
                 'quantity':int(match.group(1).replace(',',''))})
   seen.add(product)
 if not items:
  return None
 date_match=re.search(r'(20\d{2})[-年/](\d{1,2})[-月/](\d{1,2})日?', content)
 backfill=bool(re.search(
  r'补\s*(?:报|录|发|昨天|昨日|前天|20\d{2}[-年/]\d{1,2}[-月/]\d{1,2}日?|\d{1,2}月\d{1,2}日?)',
  content,
 ))
 notes=['固定格式报数已使用本地规则化解析，未调用模型']
 if aliases_used:
  notes.append('已自动规范产品名称：'+'、'.join(aliases_used))
 if date_match:
  production_date=f'{date_match.group(1)}-{int(date_match.group(2)):02d}-{int(date_match.group(3)):02d}'
 else:
  production_date=auto_production_date(event)
  notes.append('生产日期未在原消息中写明，已按消息时间自动补入生产日 '+production_date)
 return {'agent':'规则化报数解析','draft_only':True,
         'extracted':{'kind':'report','worker':worker,
                      'production_date':production_date,'items':items,
                      'missing':[p for p in PRODUCTS if p not in seen],
                      'backfill':backfill,
                      'notes':notes}}

def extract_job(job):
 """Use a deterministic parser for standard reports, Hermes only as fallback."""
 event=json.loads(job['event']) if isinstance(job.get('event'), str) else job['event']
 local=fast_extract(event)
 if local:
  return local
 r=subprocess.run([PYTHON,str(HERE/'hermes_extract.py')],input=json.dumps(event,ensure_ascii=False),
                  text=True,capture_output=True,timeout=120,cwd=HERE)
 if r.returncode: raise RuntimeError('Hermes extractor failed; check provider availability')
 return json.loads(r.stdout)

def process(job):
 try:
  data=extract_job(job);q.finish(DB,job['id'],job['lease'],data)
  if (data.get('extracted') or {}).get('kind') == 'report': recovery_scan.scan(DB)
 except Exception as e:q.finish(DB,job['id'],job['lease'],{},error=type(e).__name__+': '+str(e)[:180])

def serve():
 q.init(DB)
 recovery_scan.scan(DB)
 last_recovery=time.time()
 with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
  active=set()
  while True:
   now=time.time()
   if recovery_due(last_recovery,now):
    recovery_scan.scan(DB)
    # Automatic missing-date reminders are due at the next calendar day's
    # noon.  Delivery is idempotent and failure is held for the next cycle;
    # manual backfill messages continue through the normal inbox flow.
    try:
     missing_alerts.deliver_scheduled_alerts(DB)
    except Exception as exc:
     print('scheduled alert error',type(exc).__name__,flush=True)
    try:
     status_sync = missing_alerts.sync_pending_queue_to_github(DB)
     for item in status_sync:
      if item.get('status') == 'failed':
       print('status sync error', item.get('year'), item.get('error'), flush=True)
      elif item.get('status') == 'verified':
       print('status sync verified', item.get('year'), item.get('commit'), flush=True)
    except Exception as exc:
     print('status sync error',type(exc).__name__,flush=True)
    last_recovery=now
   for f in list(active):
    if f.done():
     try:f.result()
     except Exception as e:print('job error',type(e).__name__,flush=True)
     active.remove(f)
   while len(active)<2:
    job=q.claim(DB)
    if not job:break
    active.add(pool.submit(process,job))
   time.sleep(.5)

if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('command',choices=['ingest','show','serve','recover']);p.add_argument('message_id',nargs='?');a=p.parse_args()
 if a.command=='ingest':print(json.dumps({'received':q.put(DB,json.load(sys.stdin)),'uploaded':False}))
 elif a.command=='show':print(json.dumps(q.get(DB,a.message_id),ensure_ascii=False))
 elif a.command=='recover': print(json.dumps({'created': recovery_scan.scan(DB)},ensure_ascii=False))
 else:serve()
