"""Single local writer, optimistic remote revision guard; accepts reviewed full markdown."""
import argparse,fcntl,json,subprocess,base64,sys,re,tempfile,hashlib,os,time
from pathlib import Path
import queue_store as q
from service import DB
LEDGER_ENDPOINTS = {
    2026: 'repos/xianer-AI/xianer-ai-brain/contents/袜子生产制造袜子厂/库存记录/2026下半年下机白胚半成品统计.md',
    2027: 'repos/xianer-AI/xianer-ai-brain/contents/袜子生产制造袜子厂/库存记录/2027全年下机白胚半成品统计.md',
}


def endpoint_for_date(value):
 """Return the ledger endpoint for a validated ``YYYY-MM-DD`` date.

 A caller may override the endpoint for a controlled migration or isolated
 test.  The override is deliberately process-local; ordinary uploads still
 route 2026 and 2027 to their separate ledgers.
 """
 override = os.environ.get('PRODUCTION_LEDGER_ENDPOINT', '').strip()
 if override:
  return override
 match = re.fullmatch(r'(\d{4})-\d{2}-\d{2}', str(value or ''))
 if not match:
  raise ValueError('生产日期无效，无法选择台账')
 year = int(match.group(1))
 try:
  return LEDGER_ENDPOINTS[year]
 except KeyError:
  raise ValueError(f'暂不支持 {year} 年生产台账路由')


ENDPOINT = os.environ.get('PRODUCTION_LEDGER_ENDPOINT', LEDGER_ENDPOINTS[2026])
LEDGER_FILENAME=Path(ENDPOINT).name
VERSION_PATH=Path(__file__).with_name('VERSION.json')
LEDGER_GUARD_PATH=os.environ.get('PRODUCTION_LEDGER_GUARD','')
RISK=re.compile(r'[0-9零〇一二两三四五六七八九十百千万]|袜|下机|烤边|拷边|数量|产量|报数|更正|修改|改成|撤销|取消|暂停|算|少|多|补|不对|错误|不准确|不正确|重来|批次|上传|上报|日期|白班|晚班|夜班|工序|规格|昨天|今天|明天')
SOCIAL_HINT=re.compile(r'谢谢|辛苦|吃|饭|面条|天气|笑话|开心|好玩|晚安|早安|哈哈|周末|休息')
IDENTITY_MAPPING=re.compile(r'^[ABCD]\s*(?:=|：|:)\s*[^\s]+$')
IDENTITY_CODE=re.compile(r'^[ABCD]$')

PRODUCTS=('棉堆堆袜','冰冰袜','小腿袜','过膝袜','女船袜','男船袜')

GH='/opt/homebrew/bin/gh'
GH_READ_TIMEOUT=15
GH_READ_DELAYS=(1, 3)

def _transient_gh_error(detail):
 text=str(detail or '').lower()
 return any(token in text for token in (
  'eof','socket','timed out','timeout','connection reset','connection refused',
  'network','502','503','504','temporary failure',
 ))

def gh_read(args, *, timeout=GH_READ_TIMEOUT):
 """Run a read-only gh api call with bounded transient retries.

 PUTs deliberately do not use this helper: an ambiguous write must be
 verified remotely before any retry.  Read retries are safe because they do
 not mutate the ledger and they keep a single caller from sitting behind an
 unbounded gh process.
 """
 last=None
 for attempt in range(len(GH_READ_DELAYS) + 1):
  try:
   return subprocess.check_output([GH, *args], text=True, timeout=timeout,
                                  stderr=subprocess.STDOUT)
  except subprocess.TimeoutExpired as exc:
   last=exc
  except subprocess.CalledProcessError as exc:
   detail=exc.output or str(exc)
   if not _transient_gh_error(detail) or attempt == len(GH_READ_DELAYS):
    raise
   last=exc
  if attempt < len(GH_READ_DELAYS):
   time.sleep(GH_READ_DELAYS[attempt])
 if last:
  raise last
 raise RuntimeError('gh api 读取失败')

def gh_read_json(endpoint, *, timeout=GH_READ_TIMEOUT):
 return json.loads(gh_read(['api', endpoint], timeout=timeout))

def workbench_version():
 with VERSION_PATH.open(encoding='utf-8') as f:
  version=json.load(f).get('workbench_version')
 if not version:
  raise ValueError('运行时版本清单缺少 workbench_version')
 return version

def format_success_receipt(report, source, confirmation, commit, reread_note):
 """Render the mandatory post-upload receipt shared by all employees."""
 if not source or not confirmation or not commit or not reread_note:
  raise ValueError('成功回执缺少来源、确认、commit或远程回读凭证')
 from worker_identity import NAMES
 worker=report.get('worker') or '待核实'
 employee=f'{worker}（{NAMES[worker]}）' if worker in NAMES else worker
 items={x.get('product'): x.get('quantity') for x in (report.get('items') or [])}
 lines=['已确认并上传成功。',
        '',f"• 员工：{employee}",
        f"• 生产日：{'**' if report.get('backfill') else ''}{report.get('production_date') or '待核实'}{'**' if report.get('backfill') else ''}",
        *( ["• 记录类型：**补报**"] if report.get('backfill') else
           (["• 记录类型：**已确认未上班**"] if report.get('not_worked') else
            (["• 记录类型：**已报待查**"] if report.get('already_reported') else [])) ),
        f"• 工序：{report.get('process') or '待核实'}",'']
 if report.get('status_only'):
  lines += ['• 处理结果：已记录出勤状态，未写入生产明细、产量或累计', '']
 else:
  total=report.get('total')
  if total is None: total=sum(int(items[p]) for p in PRODUCTS if p in items)
  for product in PRODUCTS:
   lines.append(f"• {product}：{items.get(product, 0)} 双")
  lines += [f'• 合计：{total} 双','']
 lines += [
           f'• 原始报数消息：{source}',
           f'• 确认消息：{confirmation}',
           f'• GitHub commit：{commit}',
           '',f'已回读 GitHub 核实：{reread_note}',
           '', 'GitHub 回执',
           f'已更新：{Path(endpoint_for_date(report["production_date"])).name if report.get("production_date") else LEDGER_FILENAME}',
           '同步验收：全部一致',
           f'生产统计工作台版本：{workbench_version()}',
           f'远程 commit：{commit}']
 return '\n'.join(lines)

def report_from_inbox_row(row):
 """Build the receipt report from a queue-store row returned by q.get()."""
 result=row.get('result') if row else None
 if isinstance(result,str):
  result=json.loads(result)
 extracted=(result or {}).get('extracted') or {}
 items=list(extracted.get('items') or [])
 attendance_status = extracted.get('attendance_status')
 if not attendance_status:
  if any('当天未上班' in str(note) for note in extracted.get('notes') or []):
   attendance_status = 'not_worked'
  elif any('已经报过' in str(note) for note in extracted.get('notes') or []):
   attendance_status = 'already_reported'
 report={'worker':extracted.get('worker'),
         'production_date':extracted.get('production_date'),
         'kind':extracted.get('kind'),
         'backfill':bool(extracted.get('backfill')),
         'notes':list(extracted.get('notes') or []),
         'process':(items[0].get('process') if items else None),
         'items':items,
         'attendance_status': attendance_status,
         'status_only': bool(extracted.get('status_only') or attendance_status),
         'already_reported': attendance_status == 'already_reported'}
 report['not_worked'] = attendance_status == 'not_worked'
 reported={item.get('product') for item in items}
 for product in ('棉堆堆袜','冰冰袜','小腿袜','过膝袜','女船袜','男船袜'):
  if product not in reported:
   report['items'].append({'product':product,'quantity':0,'process':report['process']})
 report['total']=sum(int(x.get('quantity',0)) for x in items)
 return report
def harmless_chat(row):
 event=json.loads(row['event']) if isinstance(row['event'],str) else row['event']
 text=event.get('content','').strip().strip('！!。,.， ')
 if text in ('谢谢','谢谢你','辛苦了','哈哈','早上好','晚上好','晚安','早安','好的','收到'):return True
 if IDENTITY_MAPPING.fullmatch(text):return True
 if IDENTITY_CODE.fullmatch(text):return True
 # Day words alone are harmless in clearly social context, but no quantities/correction words may pass.
 check=re.sub(r'今天|昨天|明天','',text)
 if RISK.search(check):return False
 result=json.loads(row['result']) if isinstance(row.get('result'),str) else row.get('result')
 return bool(row.get('status')=='ready' and result and result.get('extracted',{}).get('kind')=='other' and SOCIAL_HINT.search(text))
def blocking_message(row):
 event=json.loads(row['event']) if isinstance(row.get('event'),str) else row.get('event',{})
 # Interactive backfill buttons are control-plane events.  Their text
 # contains words such as “生产补报”, but they are not a new production
 # report or correction and must never invalidate the employee's pending
 # confirmation card.
 message_id=str(event.get('messageId') or row.get('id') or '')
 if message_id.startswith('card-action-'):
  return False
 text=str(event.get('content') or event.get('text') or '').strip().strip('！!。,.， ')
 if not text or re.fullmatch(r'准确(?:\s+om_[A-Za-z0-9_-]+)?',text): return False
 if IDENTITY_MAPPING.fullmatch(text) or IDENTITY_CODE.fullmatch(text): return False
 kind=extracted_kind(row)
 if kind in ('confirmation','other') and not RISK.search(re.sub(r'谢谢|收到|好的','',text)): return False
 return kind in ('report','correction') or bool(RISK.search(text))

def extracted_kind(row):
 result=row.get('result') or {}
 if isinstance(result,str):result=json.loads(result)
 return (result.get('extracted') or {}).get('kind')

def blocks_batch(row, source_date):
 """Return whether an intervening message changes the batch being confirmed.

 Later reports for a different production day are separate batches, and
 confirmation chatter (including ``确认上传``) must never invalidate the
 current batch. Same-day reports/corrections and undated risky messages still
 block until the employee rechecks the card.
 """
 if not blocking_message(row):
  return False
 result=row.get('result') or {}
 if isinstance(result,str):result=json.loads(result)
 extracted=result.get('extracted') or {}
 kind=extracted.get('kind')
 if kind=='confirmation':
  return False
 candidate_date=extracted.get('production_date')
 if source_date and candidate_date and candidate_date != source_date and kind in ('report','correction'):
  return False
 return True


def run_original_ledger_guard(before, after):
 """Run the repository's original invariant checker before any remote PUT.

 The checker is fetched from the same remote main snapshot used for the
 ledger.  A local override is supported for isolated tests and deployments.
 """
 with tempfile.TemporaryDirectory(prefix='production-ledger-guard-') as d:
  before_path=Path(d)/'before.md'; after_path=Path(d)/'candidate.md'
  before_path.write_text(before,encoding='utf-8'); after_path.write_text(after,encoding='utf-8')
  guard_path=Path(LEDGER_GUARD_PATH) if LEDGER_GUARD_PATH else Path(d)/'guard_production_ledger.py'
  if not LEDGER_GUARD_PATH:
   raw=gh_read(['api',
    'repos/xianer-AI/xianer-ai-brain/contents/袜子生产制造袜子厂/guard_production_ledger.py?ref=main',
    '--jq','.content'])
   guard_path.write_bytes(base64.b64decode(raw))
  r=subprocess.run([sys.executable,str(guard_path),str(before_path),str(after_path)],
                   text=True,capture_output=True)
  if r.returncode:
   detail=(r.stderr.strip() or r.stdout.strip() or '未知校验错误')
   raise ValueError('原有 guard_production_ledger.py 拦截候选：'+detail)
  return (r.stdout.strip() or 'guard_production_ledger.py 校验通过')

def validate(db,source_id,confirmation_id):
 a=q.get(db,source_id);b=q.get(db,confirmation_id)
 if not a or not b:raise ValueError('报数或确认消息未在收件箱中找到')
 if a['status']!='ready' or extracted_kind(a)!='report':raise ValueError('原报数尚未完成结构化核对')
 if b['status']!='ready' or extracted_kind(b)!='confirmation':raise ValueError('确认消息尚未完成结构化核对')
 if '不上传台账' in (a['event'].get('content') or ''):raise ValueError('测试展示消息不允许写入远程台账')
 if a['grp']!=b['grp']:raise ValueError('确认来源群不匹配')
 owner='ou_de130236fb86ee826e0f5653f05bc9c6'
 extracted=(json.loads(a['result']) if isinstance(a.get('result'),str) else a.get('result') or {}).get('extracted') or {}
 proxy=bool(extracted.get('proxy')) and a['sender']==owner and extracted.get('worker') in ('A','B','C','D')
 from worker_identity import lookup
 identity=lookup(db,a['sender'])
 if not identity and not proxy:
  raise ValueError('发送账号尚未绑定员工代号，不能上传；先完成一次账号绑定')
 if identity and extracted.get('worker') in ('A','B','C','D') and extracted['worker'] != identity['worker']:
  raise ValueError('报数中的员工代号与已绑定发送账号不一致，请先核实')
 if a['sender']!=b['sender'] and b['sender']!=owner:raise ValueError('不是本人或老板确认')
 if b['created']<=a['created']:raise ValueError('确认早于报数')
 text=b['event']['content'].strip().strip('！!。,.， ')
 if confirmation_id.startswith('card-action-'):
  from review_cards import confirmation
  card=confirmation(db,source_id,confirmation_id)
  if card['sender']!=b['sender'] or card['grp']!=b['grp']:raise ValueError('卡片确认身份不匹配')
 elif re.fullmatch(r'准确(?:\s+om_[A-Za-z0-9_-]+)?',text):
  # A bare employee reply is valid only after review_cards.act_text has
  # durably associated this exact confirmation with the current source.
  if text != '准确' and text != '准确 '+source_id:
   raise ValueError('确认消息中的原始报数消息 ID 与当前批次不一致')
  from review_cards import confirmation as card_confirmation
  card_confirmation(db,source_id,confirmation_id)
 elif not re.fullmatch(r'准确\s+'+re.escape(source_id),text):
  raise ValueError('请核对当前批次后回复：准确 '+source_id+'；普通聊天赞同不能作为上传确认')
 # Only substantive production changes for the same production day block;
 # later dates are separate batches and confirmations/system chatter do not.
 source_result=a.get('result') or {}
 if isinstance(source_result,str):source_result=json.loads(source_result)
 source_date=(source_result.get('extracted') or {}).get('production_date')
 with q.conn(db) as c:
  rows=c.execute("SELECT event,status,result FROM inbox WHERE sender=? AND grp=? AND created>? AND created<?",(a['sender'],a['grp'],a['created'],b['created'])).fetchall()
 for row in rows:
  if blocks_batch(dict(row),source_date):
   raise ValueError('期间有补报、更正或待判断消息，请重新核对本批次')
 return a,b

def write_remote(a,content,source,confirmation,commit_message,receipt_name,require_absent,
                 expected_sha,review_note):
 if source not in content:raise ValueError('候选台账缺少来源消息编号')
 report=report_from_inbox_row(q.get(DB,source))
 endpoint=endpoint_for_date(report.get('production_date'))
 lock=Path(DB).parent/'ledger-write.lock'
 with lock.open('a') as f:
  fcntl.flock(f,fcntl.LOCK_EX)
  current=gh_read_json(endpoint)
  if current['sha']!=expected_sha:raise ValueError('远程已更新：请重新读取、合并、核对，不得覆盖')
  existing=base64.b64decode(current.get('content','')).decode()
  if require_absent and source in existing:raise ValueError('来源消息已在远程台账中；禁止重复入账，更正须另行关联核对')
  guard_note=run_original_ledger_guard(existing,content)
  # Re-read immediately before the write.  A candidate based on an older
  # snapshot is rejected; callers must rebuild it from the new snapshot.
  latest=gh_read_json(endpoint)
  if latest['sha']!=expected_sha:
   raise ValueError('提交前远程已更新：必须重新读取、合并、校验，禁止覆盖旧候选')
  payload={'message':commit_message,'sha':expected_sha,'branch':'main','content':base64.b64encode(content.encode()).decode()}
  r=subprocess.run(['/opt/homebrew/bin/gh','api','--method','PUT',endpoint,'--input','-'],input=json.dumps(payload),text=True,capture_output=True)
  if r.returncode:raise RuntimeError('上传失败，保持待上传状态')
  result=json.loads(r.stdout)
  commit=result.get('commit',{}).get('sha')
  if not commit: raise RuntimeError('远程提交未返回 commit，状态不明，禁止重试')
  # Persist the uncertain state before reread.  If the following GET fails,
  # recovery can inspect this receipt instead of creating a duplicate write.
  receipts=Path(DB).parent/'receipts';receipts.mkdir(exist_ok=True)
  receipt_path=receipts/receipt_name
  receipt_path.write_text(json.dumps({'source':source,'confirmation':confirmation,
    'commit':commit,'status':'committed_pending_reread','guard':guard_note,'endpoint':endpoint,
    },ensure_ascii=False))
  try:
   v=gh_read_json(endpoint)
  except Exception as exc:
   raise RuntimeError(f'已提交 {commit}，回读未核实：{exc}')
  if base64.b64decode(v['content']).decode()!=content:raise ValueError('回读与候选不一致，需核实远程最新提交')
  receipt={'source':source,'confirmation':confirmation,'commit':commit,'note':review_note,
           'status':'verified','guard':guard_note,'endpoint':endpoint,
           'backfill': bool(report.get('backfill')), 'report': report}
  try:
   report=report_from_inbox_row(q.get(DB,source))
   receipt['message']=format_success_receipt(report,source,confirmation,result['commit']['sha'],review_note)
  except Exception:
   receipt['message']=None
  receipts=Path(DB).parent/'receipts';receipts.mkdir(exist_ok=True)
  receipt_path.write_text(json.dumps(receipt,ensure_ascii=False))
  print(json.dumps(receipt,ensure_ascii=False))

def recover_pending_receipt(source,confirmation):
 """Finish a commit whose PUT succeeded but whose immediate GET was interrupted."""
 import hashlib
 path=Path(DB).parent/'receipts'/(hashlib.sha256(source.encode()).hexdigest()+'.json')
 if not path.exists(): raise ValueError('没有找到待恢复的上传回执')
 receipt=json.loads(path.read_text())
 if receipt.get('source')!=source or receipt.get('confirmation')!=confirmation:
  raise ValueError('待恢复回执与当前批次不匹配')
 if receipt.get('status')=='verified': return receipt
 if receipt.get('status')!='committed_pending_reread':
  raise ValueError('上传回执不是待回读状态')
 # Retain the actual write target across a process restart. Historical
 # receipts without that field derive it from their original production day.
 endpoint=receipt.get('endpoint') or endpoint_for_date(report_from_inbox_row(q.get(DB,source)).get('production_date'))
 current=gh_read_json(endpoint)
 content=base64.b64decode(current.get('content','')).decode()
 if source not in content or confirmation not in content:
  raise ValueError('远程台账未同时找到原始报数和确认消息')
 committed=subprocess.check_output(['/opt/homebrew/bin/gh','api',f'repos/xianer-AI/xianer-ai-brain/commits/{receipt.get("commit")}','--jq','.sha'],text=True,timeout=30).strip()
 if committed!=receipt.get('commit'):
  raise ValueError('远程 commit 不存在或不匹配')
 report = report_from_inbox_row(q.get(DB, source))
 note = f'恢复回读成功：远程台账已包含来源 {source} 和确认 {confirmation}'
 receipt.update({'status':'verified','note':note, 'backfill': bool(report.get('backfill')), 'report': report,
                 'message': format_success_receipt(
                     report, source, confirmation, receipt.get('commit'), note)})
 path.write_text(json.dumps(receipt,ensure_ascii=False))
 return receipt

def main():
 p=argparse.ArgumentParser();p.add_argument('--mode',choices=['new','correction'],default='new');p.add_argument('--source',required=True);p.add_argument('--confirmation',required=True);p.add_argument('--expected-sha',required=True);p.add_argument('--file',required=True);p.add_argument('--review-note',required=True);a=p.parse_args()
 content=Path(a.file).read_text()
 if a.mode=='new':
  try:
   recovered=recover_pending_receipt(a.source,a.confirmation)
   print(json.dumps(recovered,ensure_ascii=False));return
  except ValueError:
   pass
  validate(DB,a.source,a.confirmation)
  import hashlib
  write_remote(a,content,a.source,a.confirmation,'确认生产报数 '+a.source,hashlib.sha256(a.source.encode()).hexdigest()+'.json',True,a.expected_sha,a.review_note)
 else:
  original=q.get(DB,a.source);confirm=q.get(DB,a.confirmation)
  if not original or not confirm:raise ValueError('更正关联的原报数或确认消息未在收件箱中找到')
  if original['status']!='ready' or extracted_kind(original)!='report':raise ValueError('原报数尚未完成结构化核对')
  if confirm['status']!='ready' or extracted_kind(confirm)!='confirmation':raise ValueError('原确认消息尚未完成结构化核对')
  if original['grp']!=confirm['grp'] or original['sender']!=confirm['sender']:raise ValueError('更正关联的确认身份不匹配')
  import hashlib
  key='correction:'+a.source+':'+a.confirmation
  write_remote(a,content,a.source,a.confirmation,'更正生产报数 '+a.source,hashlib.sha256(key.encode()).hexdigest()+'.json',False,a.expected_sha,a.review_note)
if __name__=='__main__':main()
