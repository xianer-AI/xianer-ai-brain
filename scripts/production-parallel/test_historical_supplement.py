import unittest
from historical_supplement import preview, merge_extracted, apply_missing
from unittest.mock import patch
import deterministic_upload as writer

FIXTURE='''## 每日汇总
### 2026年9月每日汇总
#### 2026-09-20
| A｜徐超超 | 下机 | 2400 | 1500 | 300 | 核实 | 核实 | 核实 | 4200 |
| C｜李鸿玉 | 烤边 | 4900 | 6500 | 600 | 核实 | 3400 | 核实 | 15400 |
#### 2026-09-21
| A｜徐超超 | 下机 | 2400 | 1600 | 300 | 核实 | 0 | 0 | 4300 |
## A｜徐超超下机
### 2026-09-20｜徐超超下机
| 20260920-A-001 | 棉堆堆袜 | 2400 | 已确认 |
| 20260920-A-002 | 冰冰袜 | 1500 | 已确认 |
| 20260920-A-003 | 小腿袜 | 300 | 已确认 |
### 2026-09-21｜徐超超下机
| 20260921-A-004 | 棉堆堆袜 | 2400 | 已确认 |
| 20260921-A-005 | 冰冰袜 | 1600 | 已确认 |
| 20260921-A-006 | 小腿袜 | 300 | 已确认 |
| 20260921-A-007 | 女船袜 | 0 | 已确认 |
| 20260921-A-008 | 男船袜 | 0 | 已确认 |
## C｜李鸿玉烤边
### 2026-09-20｜李鸿玉烤边
| 20260920-C-001 | 棉堆堆袜 | 4900 | 已确认 |
| 20260920-C-002 | 冰冰袜 | 6500 | 已确认 |
| 20260920-C-003 | 小腿袜 | 600 | 已确认 |
| 20260920-C-004 | 女船袜 | 3400 | 已确认 |
'''
class SupplementTests(unittest.TestCase):
 def test_three_zeros_preserve_existing(self):
  r=preview(FIXTURE,'A','2026-09-20',{'过膝袜':0,'女船袜':0,'男船袜':0})
  self.assertEqual(list(r['values'].values()),[2400,1500,300,0,0,0])
  self.assertEqual(r['total'],4200)
 def test_sep21_one_zero(self):
  self.assertEqual(preview(FIXTURE,'A','2026-09-21',{'过膝袜':0})['total'],4300)
 def test_li_two_zeros_preserve_four(self):
  self.assertEqual(preview(FIXTURE,'C','2026-09-20',{'过膝袜':0,'男船袜':0})['total'],15400)
 def test_positive_is_not_coerced_zero(self):
  self.assertEqual(preview(FIXTURE,'A','2026-09-21',{'过膝袜':250})['total'],4550)
 def test_incomplete_or_overwrite_rejected(self):
  for v in [{'过膝袜':0},{'过膝袜':0,'女船袜':0,'男船袜':0,'棉堆堆袜':0}]:
   with self.assertRaises(ValueError): preview(FIXTURE,'A','2026-09-20',v)
 def test_wrong_sender_rejected(self):
  with self.assertRaises(ValueError): merge_extracted(FIXTURE,{'worker':'A','production_date':'2026-09-21','items':[{'product':'过膝袜','quantity':0}]},{'worker':'C'})
 def test_ambiguous_day_rejected(self):
  with self.assertRaises(ValueError): preview(FIXTURE+FIXTURE,'A','2026-09-21',{'过膝袜':0})
 def test_only_missing_detail_inserted(self):
  after,r=apply_missing(FIXTURE,'A','2026-09-20',{'过膝袜':0,'女船袜':0,'男船袜':0},'source','confirm')
  self.assertIn('| 20260920-A-001 | 棉堆堆袜 | 2400 | 已确认 |',after)
  self.assertEqual(after.split('## C｜')[1],FIXTURE.split('## C｜')[1])
  with self.assertRaises(ValueError): apply_missing(after,'A','2026-09-20',{'过膝袜':0,'女船袜':0,'男船袜':0},'again','again')
 def test_merge_keeps_date_and_all_six(self):
  e=merge_extracted(FIXTURE,{'worker':'A','production_date':'2026-09-21','items':[{'product':'过膝袜','quantity':0}]},{'worker':'A'})
  self.assertEqual(e['production_date'],'2026-09-21');self.assertEqual(len(e['items']),6);self.assertEqual(e['total'],4300)

class RecoveryIntegrationTests(unittest.TestCase):
 def test_recovery_merges_once_and_blocks_incomplete(self):
  import tempfile,json,base64
  from pathlib import Path
  import queue_store as q, recovery_scan
  for worker,day,quantities,total in [('A','2026-09-20',{'过膝袜':0,'女船袜':0,'男船袜':0},4200),('A','2026-09-21',{'过膝袜':0},4300),('C','2026-09-20',{'过膝袜':0,'男船袜':0},15400),('A','2026-09-20',{'过膝袜':0},None)]:
   with self.subTest(worker=worker,day=day,total=total),tempfile.TemporaryDirectory() as d:
    db=str(Path(d)/'inbox.sqlite');q.init(db)
    q.put(db,{'messageId':'om_fixture','senderId':'ou_fixture','groupId':recovery_scan.GROUP,'content':day+' '+str(quantities)})
    e={'kind':'report','worker':worker,'production_date':day,'items':[{'product':p,'quantity':v} for p,v in quantities.items()]}
    with q.conn(db) as c:c.execute("UPDATE inbox SET status='ready',result=?",(json.dumps({'extracted':e}),))
    with patch('worker_identity.lookup',return_value={'worker':worker,'name':'测试员工'}),patch('commit_guard.gh_read_json',return_value={'content':base64.b64encode(FIXTURE.encode()).decode()}),patch('recovery_scan.deliver') as deliver:
     recovery_scan.scan(db)
     q.put(db,{'messageId':'om_duplicate','senderId':'ou_fixture','groupId':recovery_scan.GROUP,'content':day+' '+str(quantities)})
     with q.conn(db) as c:c.execute("UPDATE inbox SET status='ready',result=? WHERE id='om_duplicate'",(json.dumps({'extracted':e}),))
     recovery_scan.scan(db)
    with q.conn(db) as c:
     cards=c.execute('SELECT summary FROM review_cards').fetchall()
     row=c.execute('SELECT result,error FROM inbox').fetchone()
    if total is None:
     self.assertEqual(len(cards),0);self.assertIn('历史补核暂停',row['error'])
    else:
     self.assertEqual(len(cards),1);r=json.loads(row['result']);self.assertEqual(r['extracted']['total'],total)
     self.assertEqual(r['original_extracted'],e);self.assertIn('不是今天产能',cards[0]['summary'])
 def test_ambiguous_confirmation_does_not_pick_latest(self):
  import tempfile,time
  from pathlib import Path
  import review_cards,queue_store as q
  with tempfile.TemporaryDirectory() as d:
   db=str(Path(d)/'inbox.sqlite');q.init(db);review_cards.init(db)
   with q.conn(db) as c:
    for n in (1,2):c.execute('INSERT INTO review_cards(token,source,sender,grp,summary,digest,expires,state) VALUES(?,?,?,?,?,?,?,?)',(str(n),'om_'+str(n),'ou_test',review_cards.GROUP,'summary','digest',time.time()+1000,'pending'))
   with self.assertRaisesRegex(ValueError,'多张'):review_cards.act_text(db,'准确','ou_test',review_cards.GROUP,'om_confirm')
   with q.conn(db) as c:self.assertEqual(c.execute("SELECT COUNT(*) FROM review_cards WHERE state='confirmed'").fetchone()[0],0)

 def test_real_reply_parser_preserves_explicit_date(self):
  import service
  with patch('worker_identity.lookup',return_value={'worker':'A'}):
   e=service.fast_extract({'senderId':'ou_fixture','content':'2026-09-20过膝袜0，女船袜0，男船袜0'})['extracted']
  self.assertEqual(e['production_date'],'2026-09-20')
  r=merge_extracted(FIXTURE,e,{'worker':'A'})
  self.assertEqual(r['total'],4200)
 def test_recovery_receipt_requires_actual_six_values(self):
  report={'worker':'A','production_date':'2026-09-20','values':preview(FIXTURE,'A','2026-09-20',{'过膝袜':0,'女船袜':0,'男船袜':0})['values'],'historical_supplement':{'过膝袜':0,'女船袜':0,'男船袜':0}}
  self.assertFalse(writer._remote_contains_exact_task(FIXTURE+' source confirm',report,'source','confirm'))
 def test_all_card_and_upload_boundaries_reject_unmerged_partial(self):
  import review_cards,commit_guard
  record={'status':'ready','event':{'content':'2026-09-20过膝袜0，女船袜0，男船袜0'},'result':{'extracted':{'kind':'report','worker':'A','production_date':'2026-09-20','items':[{'product':p,'quantity':0} for p in ('过膝袜','女船袜','男船袜')]}}}
  self.assertFalse(review_cards.ready_report(record))
  with self.assertRaisesRegex(ValueError,'禁止缺项自动补零'):commit_guard.report_from_inbox_row(record)
  record['result']['extracted']=merge_extracted(FIXTURE,record['result']['extracted'],{'worker':'A'})
  self.assertTrue(review_cards.ready_report(record))
  self.assertEqual(commit_guard.report_from_inbox_row(record)['total'],4200)
 def test_multiple_dates_cannot_be_guessed(self):
  from historical_supplement import require_bound_supplement
  with self.assertRaisesRegex(ValueError,'多个生产日期'):
   require_bound_supplement({'event':{'content':'2026-09-20 2026-10-06 过膝袜0'},'result':{'extracted':{'items':[{'product':'过膝袜','quantity':0}]}}})
 def test_model_invented_six_zero_items_from_three_fields_are_blocked(self):
  from historical_supplement import require_bound_supplement
  r={'event':{'content':'2026-09-20过膝袜0，女船袜0，男船袜0'},'result':{'extracted':{'items':[{'product':p,'quantity':0} for p in writer.PRODUCTS]}}}
  with self.assertRaisesRegex(ValueError,'禁止缺项自动补零'):require_bound_supplement(r)
 def test_correct_merged_source_cannot_send_zero_or_wrong_date_card(self):
  from historical_supplement import validate_supplement_summary
  e=merge_extracted(FIXTURE,{'worker':'A','production_date':'2026-09-20','items':[{'product':p,'quantity':0} for p in ('过膝袜','女船袜','男船袜')]},{'worker':'A'})
  record={'event':{'content':'2026-09-20过膝袜0，女船袜0，男船袜0'},'result':{'extracted':e}}
  good='生产日：2026-09-20\n'+'\n'.join(p+'：'+str(v) for p,v in preview(FIXTURE,'A','2026-09-20',{'过膝袜':0,'女船袜':0,'男船袜':0})['values'].items())+'\n合计：4200'
  validate_supplement_summary(record,good)
  for bad in [good.replace('2400','0'),good.replace('4200','0'),good.replace('2026-09-20','2026-10-06')]:
   with self.assertRaises(ValueError):validate_supplement_summary(record,bad)
 def test_stored_wrong_card_is_closed_before_delivery(self):
  import tempfile,json,time
  from pathlib import Path
  import queue_store as q,review_cards
  with tempfile.TemporaryDirectory() as d:
   db=str(Path(d)/'inbox.sqlite');q.init(db);review_cards.init(db)
   q.put(db,{'messageId':'om_partial','senderId':'ou_fixture','groupId':review_cards.GROUP,'content':'2026-09-20过膝袜0，女船袜0，男船袜0'})
   e=merge_extracted(FIXTURE,{'worker':'A','production_date':'2026-09-20','items':[{'product':p,'quantity':0} for p in ('过膝袜','女船袜','男船袜')]},{'worker':'A'})
   with q.conn(db) as c:
    c.execute("UPDATE inbox SET status='ready',result=?",(json.dumps({'extracted':e}),))
    c.execute('INSERT INTO review_cards(token,source,sender,grp,summary,digest,expires,state) VALUES(?,?,?,?,?,?,?,?)',('wrong','om_partial','ou_fixture',review_cards.GROUP,'生产日：2026-09-20\n'+'\n'.join(p+'：0' for p in writer.PRODUCTS)+'\n合计：0','digest',time.time()+1000,'pending'))
   with patch('transport.request') as send:
    r=review_cards.deliver_card(db,'wrong');send.assert_not_called()
   self.assertEqual(r['status'],'blocked')
   with q.conn(db) as c:self.assertEqual(c.execute("SELECT state FROM review_cards WHERE token='wrong'").fetchone()[0],'superseded')

 def test_worded_partial_zeros_cannot_be_expanded_to_full_zeros(self):
  from historical_supplement import require_bound_supplement
  record={'event':{'content':'2026-09-20 过膝袜、女船袜、男船袜都为零'},'result':{'extracted':{'items':[{'product':p,'quantity':0} for p in writer.PRODUCTS]}}}
  with self.assertRaises(ValueError):require_bound_supplement(record)
 def test_full_dated_report_also_rejects_wrong_zero_card(self):
  from historical_supplement import validate_supplement_summary,require_bound_supplement
  values=dict(zip(writer.PRODUCTS,[2400,1500,300,0,0,0]))
  record={'event':{'content':'2026-09-20 '+','.join(p+str(v) for p,v in values.items())},'result':{'extracted':{'production_date':'2026-09-20','items':[{'product':p,'quantity':v} for p,v in values.items()]}}}
  with self.assertRaises(ValueError):validate_supplement_summary(record,'生产日：2026-09-20\n'+'\n'.join(p+'：0' for p in values)+'\n合计：0')
  record['result']['extracted']['production_date']='2026-10-06'
  with self.assertRaisesRegex(ValueError,'解析生产日期'):require_bound_supplement(record)
 def test_deleted_bad_message_cannot_reenter_queue(self):
  import tempfile
  from pathlib import Path
  import queue_store as q
  with tempfile.TemporaryDirectory() as d:
   db=str(Path(d)/'inbox.sqlite')
   bad={'messageId':'om_retired','senderId':'ou_test','groupId':'g','content':'old invalid draft'}
   q.put(db,bad);q.block_replay(db,['om_retired'])
   with q.conn(db) as c:c.execute("DELETE FROM inbox WHERE id='om_retired'")
   self.assertIsNone(q.put(db,bad));self.assertIsNone(q.get(db,'om_retired'));self.assertIsNone(q.claim(db))
   with q.conn(db) as c:
    row=c.execute('SELECT * FROM blocked_message_fingerprints').fetchone()
    self.assertEqual(len(row['id_hash']),64);self.assertNotIn('om_retired',str(tuple(row)))
   self.assertEqual(q.put(db,dict(bad,messageId='om_fresh')), 'om_fresh')
 def test_zero_only_partial_without_date_is_not_a_zero_production_day(self):
  from historical_supplement import require_bound_supplement
  for raw in ['c 过膝袜0','过膝袜、女船袜、男船袜都为零','生产补报 3 B 2026-09-20']:
   with self.assertRaisesRegex(ValueError,'不能生成全零'):
    require_bound_supplement({'event':{'content':raw},'result':{'extracted':{'items':[{'product':p,'quantity':0} for p in writer.PRODUCTS]}}})
