"""Isolated dashboard release and served-snapshot verification regressions."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import build_production_dashboard as build


VERSION = {'workbench_version': 'V1.18', 'sync_protocol_version': 'S1',
           'card_protocol_version': 'CARD-INTERACTIVE-1', 'release_id': '2026-10-06-v1.18',
           'release_status': '正式'}
PUBLISHER = Path(__file__).with_name('publish_production_dashboard.py')


class SnapshotTests(unittest.TestCase):
    def render(self, ledger='原台账', version=None, generated_at='2026-10-06T15:00:00+00:00'):
        return build.render_snapshot('<main>原布局</main><script>run();</script>', ledger,
                                     version or VERSION, generated_at=generated_at, builder_hash='a' * 64)

    def test_release_uses_formal_version_and_exact_ledger(self):
        html, manifest, release = self.render()
        self.assertEqual(json.loads(manifest)['rules_release_id'], VERSION['release_id'])
        self.assertEqual(release['workbench_version'], 'V1.18')
        self.assertEqual(release['ledger_sha256'], build.sha256('原台账'.encode()))
        self.assertIn('window.__DASHBOARD_RELEASE__=', html.decode())
        self.assertIn('<main>原布局</main>', html.decode())

    def test_rebuild_time_alone_does_not_trigger_publish(self):
        first = self.render(generated_at='2026-10-06T15:00:00+00:00')[2]
        second = self.render(generated_at='2026-10-06T16:00:00+00:00')[2]
        self.assertEqual(first['source_digest'], second['source_digest'])

    def test_ledger_or_rule_change_changes_source_digest(self):
        first = self.render()[2]['source_digest']
        self.assertNotEqual(first, self.render(ledger='最新台账')[2]['source_digest'])
        changed = dict(VERSION, release_id='2026-10-07-v1.18')
        self.assertNotEqual(first, self.render(version=changed)[2]['source_digest'])

    def test_incomplete_release_or_bad_template_is_rejected(self):
        with self.assertRaises(ValueError):
            self.render(version={'workbench_version': 'V1.18'})
        with self.assertRaises(ValueError):
            build.render_snapshot('no script', '台账', VERSION)

    def test_ledger_cannot_close_the_data_script(self):
        html = self.render(ledger='</script><script>alert(1)</script>')[0].decode()
        self.assertEqual(html.count('<script>'), 1)
        self.assertIn('\\u003c/script>', html)

    def test_annual_snapshot_hashes_each_exact_ledger_and_updates_for_2027(self):
        annual = {'2026': '2026正式台账数量', '2027': '2027空白模版'}
        html, manifest, release = build.render_snapshot('<script>run();</script>', annual['2026'],
                                                       VERSION, ledgers=annual)
        self.assertEqual(release['available_years'], ['2026', '2027'])
        self.assertEqual(release['ledger_sha256'], build.sha256(annual['2026'].encode()))
        self.assertEqual(json.loads(manifest)['ledgers_sha256']['2027'],
                         build.sha256(annual['2027'].encode()))
        self.assertIn('window.__LEDGERS__={"2026":"2026正式台账数量","2027":"2027空白模版"}', html.decode())
        changed = build.render_snapshot('<script>run();</script>', annual['2026'], VERSION,
                                        ledgers=dict(annual, **{'2027': '2027新报数'}))[2]
        self.assertNotEqual(release['source_digest'], changed['source_digest'])
        self.assertEqual(release['ledger_sha256'], changed['ledger_sha256'])

    def test_annual_snapshot_rejects_missing_or_mismatched_legacy_ledger(self):
        for ledgers in ({'2027': '空白模板'}, {'2026': '别的台账'}, {'2026': '原台账', '2027': ''}):
            with self.subTest(ledgers=ledgers), self.assertRaises(ValueError):
                build.render_snapshot('<script>run();</script>', '原台账', VERSION, ledgers=ledgers)


class PublisherTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location('dashboard_publisher', PUBLISHER)
        cls.publisher = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.publisher)

    def test_public_readback_checks_snapshot_and_legacy_entry(self):
        module = self.publisher
        class Response:
            status = 200
            def __init__(self, body): self.body = body
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return self.body
        with patch.object(module.urllib.request, 'urlopen', side_effect=[Response(b'html'), Response(b'manifest'), Response(b'redirect'), Response(b'redirect')]) as opener:
            module.verify_public({'index.html': build.sha256(b'html'), 'release.json': build.sha256(b'manifest'),
                                  'production-dashboard.html': build.sha256(b'redirect'),
                                  '/index.html': build.sha256(b'redirect')})
        urls = [call.args[0].full_url for call in opener.call_args_list]
        self.assertIn('/index.html?_verify=', urls[0])
        self.assertIn('/release.json?_verify=', urls[1])
        self.assertIn('/production-dashboard.html?_verify=', urls[2])
        self.assertTrue(urls[3].startswith('https://tengtiao-calc-d8gpq679da44f9bc2-1497888928.tcloudbaseapp.com/index.html?_verify='))

    def test_missing_or_changed_legacy_entry_is_published_without_ledger_change(self):
        module = self.publisher
        snapshot = build.render_snapshot('<script>run();</script>', '台账', VERSION)
        for old_legacy in (None, b'old redirect'):
            with self.subTest(old_legacy=old_legacy), tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp)
                state = directory / 'state.json'
                compatibility = directory / 'production-dashboard.html'
                compatibility.write_bytes(b'latest redirect')
                old_hashes = {'index.html': build.sha256(snapshot[0]), 'release.json': build.sha256(snapshot[1])}
                if old_legacy is not None:
                    old_hashes['production-dashboard.html'] = build.sha256(old_legacy)
                state.write_text(json.dumps({'digest': snapshot[2]['source_digest'],
                                             'verified_unix': module.time.time(), 'public_sha256': old_hashes}))
                executable = directory / 'tcb'; executable.touch()

                def inspect_deploy(command, **kwargs):
                    staged = Path(command[3])
                    self.assertNotIn('--prune', command)
                    if command[4] == 'production-dashboard':
                        self.assertEqual({path.name for path in staged.iterdir()},
                                         {'index.html', 'release.json', 'production-dashboard.html'})
                        self.assertEqual((staged / 'production-dashboard.html').read_bytes(), b'latest redirect')
                    else:
                        self.assertEqual(command[4], 'index.html')
                        self.assertTrue(staged.is_file())
                        self.assertEqual(staged.read_bytes(), b'latest redirect')
                    return subprocess.CompletedProcess(command, 0, 'ok', '')

                with patch.multiple(module, STATE=state, LOCK=directory / 'lock', LOG=directory / 'log',
                                    TCB=executable, COMPATIBILITY_FILES={'production-dashboard.html': compatibility}), \
                        patch.object(module, 'capture_snapshot', return_value=snapshot), \
                        patch.object(module.subprocess, 'run', side_effect=inspect_deploy) as deploy, \
                        patch.object(module, 'verify_public') as readback:
                    self.assertEqual(module.main(), 0)
                    expected = dict(old_hashes, **{'production-dashboard.html': build.sha256(b'latest redirect'),
                                                   '/index.html': build.sha256(b'latest redirect')})
                    readback.assert_called_once_with(expected)
                    self.assertEqual(json.loads(state.read_text())['public_sha256'], expected)
                    self.assertEqual(module.main(), 0)
                    self.assertEqual(deploy.call_count, 2)
                    self.assertEqual([call.args[0][4] for call in deploy.call_args_list],
                                     ['production-dashboard', 'index.html'])

    def test_stale_online_html_is_not_success(self):
        class Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return b'old html'
        with patch.object(self.publisher.urllib.request, 'urlopen', return_value=Response()):
            with self.assertRaisesRegex(RuntimeError, 'differs'):
                self.publisher.verify_public({'index.html': build.sha256(b'new html')})

    def test_failed_publish_does_not_advance_success_state(self):
        module = self.publisher
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            state = directory / 'state.json'
            state.write_text('{"digest":"old"}')
            executable = directory / 'tcb'; executable.touch()
            snapshot = build.render_snapshot('<script>run();</script>', '台账', VERSION)
            with patch.multiple(module, STATE=state, LOCK=directory / 'lock', LOG=directory / 'log', TCB=executable), \
                    patch.object(module, 'capture_snapshot', return_value=snapshot), \
                    patch.object(module.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, 'ok', '')), \
                    patch.object(module, 'verify_public', side_effect=RuntimeError('still stale')), \
                    patch.object(module.time, 'sleep'):
                self.assertEqual(module.main(), 1)
            self.assertEqual(json.loads(state.read_text())['digest'], 'old')

    def test_failed_root_compatibility_publish_does_not_record_success(self):
        module = self.publisher
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            state = directory / 'state.json'
            state.write_text('{"digest":"old"}')
            executable = directory / 'tcb'; executable.touch()
            snapshot = build.render_snapshot('<script>run();</script>', '台账', VERSION)
            with patch.multiple(module, STATE=state, LOCK=directory / 'lock', LOG=directory / 'log', TCB=executable), \
                    patch.object(module, 'capture_snapshot', return_value=snapshot), \
                    patch.object(module.subprocess, 'run', side_effect=[
                        subprocess.CompletedProcess([], 0, 'ok', ''),
                        subprocess.CompletedProcess([], 1, '', 'single-file upload failed'),
                    ]), patch.object(module, 'verify_public') as readback:
                self.assertEqual(module.main(), 1)
                readback.assert_not_called()
            self.assertEqual(json.loads(state.read_text())['digest'], 'old')


class BrowserLogicTests(unittest.TestCase):
    def run_node(self, code):
        executable = shutil.which('node') or '/Users/xianer/.local/share/fnm/node-versions/v24.21.0/installation/bin/node'
        result = subprocess.run([executable, '-e', code], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def annual_browser(self, assertions, ledgers=None, local_storage=None, session_storage=None):
        script = build.TEMPLATE.read_text().split('<script>', 1)[1].split('\nrefresh();', 1)[0]
        fixture = ledgers or {'2026': '2026台账', '2027': '2027台账'}
        prelude = '''
const assert=require('node:assert/strict');
const storage=(initial={})=>{const data=new Map(Object.entries(initial));return {getItem:key=>data.get(key)||null,setItem:(key,value)=>data.set(key,value)};};
const window={scrollY:120,scrollTo(x,y){this.scrollY=y;},matchMedia(){return {matches:true};}};
const location={href:'https://example.test/index.html?year=2027'};
const history={replaceState(a,b,address){location.href=address;}};
const requestAnimationFrame=callback=>callback();
'''
        prelude += 'const localStorage=storage(' + json.dumps(local_storage or {}) + ');\n'
        prelude += 'const sessionStorage=storage(' + json.dumps(session_storage or {}) + ');\n'
        prelude += 'window.__LEDGERS__=' + json.dumps(fixture, ensure_ascii=False) + ';\n'
        self.run_node(prelude + script + '\n' + assertions)

    def test_display_labels_preserve_canonical_processes_and_quantities_in_both_years(self):
        self.annual_browser(r'''
assert.deepEqual(DEFAULT_WORKERS.map(worker=>worker.process),['下机','下机','烤边','烤边']);
assert.equal(processLabel('下机'),'下机翻袜产量');
assert.equal(processLabel('烤边'),'烤边产量');
assert.equal(processLabel('未知'),'待核实');
for(const year of ['2026','2027']){
  activeYear=year;
  const date=year==='2026'?'2026-10-06':'2027-01-06';
  const records=DEFAULT_WORKERS.map((worker,index)=>({date,person:worker.person,process:worker.process,values:[String((index+1)*100),'20','0','0','0','0']}));
  const before=JSON.stringify(records),statuses=new Map();
  for(const worker of DEFAULT_WORKERS){
    const record=records.find(row=>row.person===worker.person),label=processLabel(worker.process);
    for(const markup of [recordCard(worker,record,'已确认',date),summaryCards([worker],records,statuses),aggregateRow(worker,aggregate([record]),statuses),makeDailyRows(records,[worker],statuses,[date]).join('')]){
      assert.ok(markup.includes(label));
      assert.doesNotMatch(markup,/>下机<|>烤边</);
    }
  }
  assert.equal(buildProductTotals(DEFAULT_WORKERS,records,statuses,'下机',date,date).value,340);
  assert.equal(buildProductTotals(DEFAULT_WORKERS,records,statuses,'烤边',date,date).value,740);
  assert.equal(JSON.stringify(records),before);
}
''')
        template = build.TEMPLATE.read_text()
        self.assertIn('<option value="下机">下机翻袜产量</option>', template)
        self.assertIn('<option value="烤边">烤边产量</option>', template)
        self.assertIn('李鸿玉＋张小翠合计为总产量（烤边）', template)
        self.assertNotIn('下机翻袜完成量', template)
        self.assertNotIn('烤边完成量', template)
        self.assertNotIn('本地显示预览', template)

    def test_product_total_labels_keep_desktop_days_and_mobile_fold_behavior(self):
        self.annual_browser(r'''
const elements=new Map();
const element=selector=>{
  if(!elements.has(selector))elements.set(selector,{value:'',innerHTML:'',textContent:'',hidden:false,querySelectorAll(){return [];}});
  return elements.get(selector);
};
const document={querySelector:element};
for(const year of ['2026','2027']){
  activeYear=year;
  const latest=year==='2026'?'2026-10-06':'2027-01-06';
  for(const desktop of [true,false]){
    window.matchMedia=()=>({matches:desktop});
    for(const mode of ['1','3','5','7']){
      element('#product-total-range').value=mode;
      element('#product-total-process').value='烤边';
      renderProductTotals(DEFAULT_WORKERS,[],new Map(),latest);
      const markup=element('#product-total-result').innerHTML;
      assert.match(markup,/data-total-process="下机"><h3>每日下机翻袜产量/);
      assert.match(markup,/data-total-process="烤边"><h3>每日烤边产量/);
      assert.match(markup,/class="process-tag">下机翻袜产量/);
      assert.match(markup,/class="process-tag">烤边产量/);
      assert.equal(markup.includes('product-total-fold'),!desktop&&Number(mode)>3);
      assert.ok(element('#product-total-caption').textContent.includes(latest));
    }
  }
}
''')

    def test_legacy_entry_redirect_preserves_year_query_and_anchor(self):
        legacy = (build.ROOT / 'docs/production-dashboard.html').read_text()
        self.assertNotIn('__LEDGER__', legacy)
        self.assertNotIn('__DASHBOARD_RELEASE__', legacy)
        script = legacy.split('<script>', 1)[1].split('</script>', 1)[0]
        for directory, year in (('/', '2026'), ('/production-dashboard/', '2027')):
            with self.subTest(directory=directory, year=year):
                address = f'https://example.test{directory}production-dashboard.html?year={year}&_sync=123#product-total-title'
                self.run_node('const assert=require("node:assert/strict");' +
                              'const incoming=new URL(' + json.dumps(address) + ');' +
                              'const location={href:incoming.href,search:incoming.search,hash:incoming.hash,replace(value){this.replacement=value;}};' +
                              'const link={};const document={getElementById(id){assert.equal(id,"dashboard-link");return link;}};' +
                              script +
                              'const target=new URL(location.replacement);assert.equal(target.origin,"https://tengtiao-calc-d8gpq679da44f9bc2-1497888928.tcloudbaseapp.com");' +
                              'assert.equal(target.pathname,"/production-dashboard/index.html");' +
                              'assert.equal(target.search,incoming.search);assert.equal(target.hash,incoming.hash);assert.equal(link.href,target.href);')

    def test_cloudflare_root_redirect_is_host_and_path_specific_without_loop(self):
        template = build.TEMPLATE.read_text()
        self.assertEqual(template.count('<script>'), 1)
        redirect = template.split('<script>', 1)[1].split('const dashboardRelease=', 1)[0]
        self.run_node('const assert=require("node:assert/strict");' +
                      'const redirect=address=>{const location=new URL(address);location.replace=value=>location.replacement=value;' +
                      redirect + 'return location;};' + r'''
const canonical='https://tengtiao-calc-d8gpq679da44f9bc2-1497888928.tcloudbaseapp.com/production-dashboard/index.html';
for(const year of ['2026','2027']){
  for(const path of ['/','/index.html']){
    const incoming=redirect('https://xianer-ai-brain.pages.dev'+path+'?year='+year+'&_sync=123#product-total-title');
    const target=new URL(incoming.replacement);
    assert.equal(target.origin+target.pathname,canonical);
    assert.equal(target.search,incoming.search);
    assert.equal(target.hash,incoming.hash);
    assert.equal(redirect(target.href).replacement,undefined);
  }
}
for(const address of [
  canonical+'?year=2027#product-total-title',
  'https://tengtiao-calc-d8gpq679da44f9bc2-1497888928.tcloudbaseapp.com/index.html',
  'http://localhost:8795/index.html?year=2027#product-total-title',
  'http://127.0.0.1:8795/',
  'https://other.pages.dev/',
  'https://xianer-ai-brain.pages.dev.example.com/',
  'https://xianer-ai-brain.pages.dev/production-dashboard',
  'https://xianer-ai-brain.pages.dev/other-tool/index.html',
])assert.equal(redirect(address).replacement,undefined,address);
''')

    def test_display_classification_and_partial_total(self):
        self.annual_browser(r"""
assert.equal(syncDisplayState('上传失败'),'fault');
assert.equal(syncDisplayState('上传失败，已恢复'),'recovered');
assert.equal(syncDisplayState('历史异常：上传失败，已归档'),'history');
assert.equal(syncDisplayState('等待员工核实'),'pending');
assert.equal(syncDisplayState(''),'unknown');
assert.equal(productTotalCompleteness({value:100,missing:true}),'已报合计，数据未齐');
assert.equal(productTotalCompleteness({value:0,missing:false}),'已报数据完整');
assert.equal(productTotalCompleteness({value:null,missing:false}),'暂无已报数量');
""")

    def test_irregular_finishing_worker_does_not_create_missing_alerts(self):
        self.annual_browser(r"""
const workers=[{code:'C',person:'C',process:'烤边'},{code:'D',person:'D',process:'烤边'}];
const rec={date:'2026-10-06',person:'C',process:'烤边',values:['100','0','0','0','0','0']};
let result=buildProductTotals(workers,[rec],new Map(),'烤边','2026-10-06','2026-10-06');
assert.equal(result.value,100);assert.equal(result.missing,false);
result=buildProductTotals(workers,[rec,{date:rec.date,person:'D',process:'烤边',values:['20','0','0','0','0','0']}],new Map(),'烤边',rec.date,rec.date);
assert.equal(result.value,120);assert.equal(result.missing,false);
result=buildProductTotals(workers,[rec],new Map([[rec.date+'|D','待核实']]),'烤边',rec.date,rec.date);
assert.equal(result.missing,true);
result=buildProductTotals(workers,[],new Map(),'烤边',rec.date,rec.date);
assert.equal(result.value,null);assert.equal(result.missing,true);
""")

    def test_product_totals_cutoff_is_beijing_yesterday(self):
        self.annual_browser(r"""
assert.equal(productTotalsCutoff('2026',new Date('2026-10-07T10:00:00Z')),'2026-10-06');
assert.equal(productTotalsCutoff('2026',new Date('2026-10-07T16:00:00Z')),'2026-10-07');
assert.equal(productTotalsCutoff('2026',new Date('2026-10-07T15:59:59Z')),'2026-10-06');
assert.equal(productTotalsCutoff('2026',new Date('2026-10-01T01:00:00Z')),'2026-09-30');
assert.equal(productTotalsCutoff('2026',new Date('2027-01-01T01:00:00Z')),'2026-12-31');
assert.equal(productTotalsCutoff('2027',new Date('2026-10-07T10:00:00Z')),'2026-10-06');
""")

    def test_product_totals_keep_processes_missing_days_and_calendar_range(self):
        self.annual_browser(r"""
const workers=[{person:'A',process:'下机'},{person:'B',process:'下机'},{person:'C',process:'烤边'}];
const records=[{date:'2026-09-30',person:'A',process:'下机',values:['100','0','待核实','0','0','0']},
{date:'2026-09-30',person:'B',process:'下机',values:['200','10','0','0','0','0']},
{date:'2026-09-30',person:'C',process:'烤边',values:['900','0','0','0','0','0']},
{date:'2026-10-01',person:'A',process:'下机',values:['50','0','0','0','0','0']}];
const states=new Map([['2026-10-01|B','未上班']]);
const result=buildProductTotals(workers,records,states,'下机','2026-09-30','2026-10-02');
assert.equal(result.days.length,3);
assert.equal(result.days[0].value,null);
assert.equal(result.days[0].missing,true);
assert.equal(result.days[1].value,50);
assert.equal(result.days[1].missing,false);
assert.equal(result.days[2].cells[0].value,300);
assert.equal(result.days[2].cells[2].missing,1);
assert.equal(result.value,360);
assert.equal(result.missing,true);
assert.equal(buildProductTotals(workers,records,states,'烤边','2026-09-30','2026-09-30').value,900);
assert.deepEqual(productTotalDays('2026-09-29','2026-10-05'),['2026-10-05','2026-10-04','2026-10-03','2026-10-02','2026-10-01','2026-09-30','2026-09-29']);
""")

    def comparison_browser(self, assertions, local_storage=None):
        self.annual_browser('''
const [workerA,workerB,workerC,workerD]=DEFAULT_WORKERS;
const record=(worker,date,values,process=worker.process)=>({date,person:worker.person,process,values:values.map(String)});
const attendance=(entries=[])=>new Map(entries.map(([worker,date,status])=>[`${date}|${worker.person}`,status]));
const options={process:'下机',start:'2027-01-01',end:'2027-01-31',products:[0,1,2,3,4,5]};
const compare=(records,statuses=new Map(),overrides={})=>buildProductionComparison(DEFAULT_WORKERS,records,statuses,{...options,...overrides});
''' + assertions, local_storage=local_storage)

    def desktop_comparison_browser(self, assertions, local_storage=None):
        self.comparison_browser(r'''
window.matchMedia=query=>({matches:query==='(min-width:701px)'});
const elements=new Map();
const element=selector=>{
  if(!elements.has(selector))elements.set(selector,{id:selector.slice(1),value:'',innerHTML:'',dataset:{},hidden:false,listeners:{},addEventListener(type,listener){this.listeners[type]=listener;}});
  return elements.get(selector);
};
let productInputs=[],productMarkup='';
Object.defineProperty(element('#comparison-products'),'innerHTML',{
  get(){return productMarkup;},set(markup){productMarkup=markup;productInputs=[...markup.matchAll(/<input type="checkbox" value="(\d+)"( checked)?>/g)].map(match=>({value:match[1],checked:!!match[2]}));}
});
const periodFields=['month','day','range'].map(mode=>({dataset:{comparisonPeriod:mode},hidden:false}));
const document={querySelector:element,getElementById:id=>element('#'+id),querySelectorAll(selector){
  if(selector==='#comparison-products input:checked')return productInputs.filter(input=>input.checked);
  if(selector==='#comparison-products input')return productInputs;
  if(selector==='[data-comparison-period]')return periodFields;
  throw Error('Unexpected selector: '+selector);
}};
''' + assertions, local_storage=local_storage)

    def test_desktop_comparison_known_equal_higher_lower_and_zero_use_real_totals(self):
        self.comparison_browser('''
const result=compare([
  record(workerA,'2027-01-01',[100,60,0,20,50,10]),
  record(workerA,'2027-01-02',[40,30,0,10,20,5]),
  record(workerB,'2027-01-01',[80,60,0,25,20,10]),
  record(workerB,'2027-01-02',[40,30,0,15,10,5])
]);
assert.equal(result.valid,true);
assert.deepEqual(result.workers.map(worker=>worker.code),['A','B']);
assert.deepEqual(result.dates,['2027-01-01','2027-01-02']);
assert.deepEqual(result.rows.map(row=>row.productIndex),[0,1,2,3,4,5]);
assert.deepEqual(result.rows.map(row=>row.left.value),[140,90,0,30,70,15]);
assert.deepEqual(result.rows.map(row=>row.right.value),[120,90,0,40,30,15]);
assert.deepEqual(result.rows.map(row=>row.difference),[20,0,0,-10,40,0]);
assert.deepEqual(result.rows.map(row=>row.combined),[260,180,0,70,100,30]);
assert.ok(result.rows.every(row=>row.complete));
assert.equal(result.rows[2].left.days,2);assert.equal(result.rows[2].left.average,0);
assert.equal(result.rows[0].left.average,70);assert.equal(result.rows[0].right.average,60);
assert.equal(result.totals.left.value,345);assert.equal(result.totals.right.value,295);
assert.equal(result.totals.combined,640);assert.equal(result.totals.difference,50);
assert.equal(result.totals.left.days,2);assert.equal(result.totals.right.days,2);
assert.equal(result.totals.left.average,172.5);assert.equal(result.totals.right.average,147.5);
assert.equal(result.totals.complete,true);assert.deepEqual(result.missing,[]);
assert.match(comparisonDifference(result.rows[0],result.workers),/class="comparison-number comparison-high">[+]20/);
assert.match(comparisonDifference(result.rows[3],result.workers),/class="comparison-number comparison-low">-10/);
assert.match(comparisonDifference(result.rows[1],result.workers),/class="comparison-number">0 · 持平/);
assert.doesNotMatch(comparisonDifference(result.rows[1],result.workers),/comparison-high|comparison-low/);
''')

    def test_desktop_comparison_absence_is_resolved_without_becoming_zero_or_production_day(self):
        self.comparison_browser('''
const result=compare([
  record(workerA,'2027-01-01',[10,0,0,0,0,0]),
  record(workerA,'2027-01-02',[30,0,0,0,0,0]),
  record(workerB,'2027-01-01',[90,0,0,0,0,0])
],attendance([
  [workerA,'2027-01-03','已确认未上班'],
  [workerB,'2027-01-02','已确认未上班'],
  [workerB,'2027-01-03','已确认未上班']
]));
assert.deepEqual(result.dates,['2027-01-01','2027-01-02','2027-01-03']);
const row=result.rows[0];
assert.equal(row.left.value,40);assert.equal(row.left.days,2);assert.equal(row.left.absentDays,1);assert.equal(row.left.average,20);
assert.equal(row.right.value,90);assert.equal(row.right.days,1);assert.equal(row.right.absentDays,2);assert.equal(row.right.average,90);
assert.equal(row.complete,true);assert.equal(row.difference,-50);assert.deepEqual(result.missing,[]);
assert.equal(row.days[1].right.value,null);assert.equal(row.days[1].right.days,0);assert.equal(row.days[1].right.status,'已确认未上班');
assert.equal(row.days[1].combined,30);assert.equal(row.days[1].difference,null);
assert.equal(row.days[2].combined,null);assert.equal(row.days[2].difference,null);
assert.equal(result.totals.left.days,2);assert.equal(result.totals.right.days,1);
const absenceOnly=compare([],attendance([[workerA,'2027-01-01','已确认未上班'],[workerB,'2027-01-01','已确认未上班']]));
for(const cell of [absenceOnly.rows[0].left,absenceOnly.rows[0].right,absenceOnly.totals.left]){
  assert.equal(cell.value,null);assert.equal(cell.days,0);assert.equal(cell.absentDays,1);
  assert.equal(cell.average,null);assert.equal(cell.complete,false);assert.deepEqual(cell.missingDates,[]);
}
assert.equal(absenceOnly.rows[0].combined,null);assert.equal(absenceOnly.rows[0].difference,null);
assert.deepEqual(absenceOnly.missing,[]);
''')

    def test_desktop_comparison_partial_values_survive_without_false_equality_or_average(self):
        self.comparison_browser('''
const result=compare([
  record(workerA,'2027-01-01',[1,'核实',0,0,0,0]),
  record(workerA,'2027-01-02',[2,10,0,0,0,0]),
  record(workerB,'2027-01-01',[1,3,0,0,0,0]),
  record(workerB,'2027-01-02',[2,4,0,0,0,0])
]);
const known=result.rows[0],partial=result.rows[1];
assert.equal(known.complete,true);assert.equal(known.difference,0);
assert.equal(partial.left.value,10);assert.equal(partial.left.days,1);assert.equal(partial.left.average,null);
assert.equal(partial.left.complete,false);
assert.deepEqual(partial.left.missingDates,[{date:'2027-01-01',status:'待核实'}]);
assert.equal(partial.right.value,7);assert.equal(partial.right.days,2);assert.equal(partial.right.average,3.5);
assert.equal(partial.combined,17);assert.equal(partial.complete,false);assert.equal(partial.difference,null);
assert.doesNotMatch(comparisonDifference(partial,result.workers),/comparison-high|comparison-low|持平/);
assert.equal(result.totals.left.value,13);assert.equal(result.totals.left.days,2);
assert.equal(result.totals.left.average,null);assert.equal(result.totals.left.complete,false);
assert.equal(result.totals.right.value,10);assert.equal(result.totals.right.days,2);assert.equal(result.totals.right.average,5);
assert.equal(result.totals.combined,23);assert.equal(result.totals.difference,null);
assert.equal(result.missing.length,1);
assert.equal(result.missing[0].worker.person,workerA.person);
assert.deepEqual({date:result.missing[0].date,productIndex:result.missing[0].productIndex,product:result.missing[0].product,status:result.missing[0].status},
                 {date:'2027-01-01',productIndex:1,product:'冰冰袜',status:'待核实'});
const unknown=compare([record(workerA,'2027-01-01',['核实','核实','核实','核实','核实','核实']),record(workerB,'2027-01-01',['核实','核实','核实','核实','核实','核实'])]);
assert.equal(unknown.rows[0].left.value,null);assert.equal(unknown.rows[0].right.value,null);
assert.equal(unknown.rows[0].combined,null);assert.equal(unknown.rows[0].difference,null);
assert.equal(unknown.totals.left.days,0);assert.equal(unknown.totals.left.average,null);
const unknownText=compare([record(workerA,'2027-01-01',['未知','—',0,0,0,0]),record(workerB,'2027-01-01',['unknown','—',0,0,0,0])]);
assert.equal(unknownText.rows[0].left.value,null);assert.equal(unknownText.rows[0].left.days,0);
assert.deepEqual(unknownText.rows[0].left.missingDates,[{date:'2027-01-01',status:'未知'}]);
assert.deepEqual(unknownText.rows[1].left.missingDates,[{date:'2027-01-01',status:'无记录'}]);
''')

    def test_desktop_comparison_pending_unreported_and_absent_are_distinct(self):
        self.comparison_browser('''
const result=compare([
  record(workerA,'2027-01-01',[0,0,0,0,0,0]),
  record(workerB,'2027-01-01',[5,0,0,0,0,0]),
  record(workerB,'2027-01-02',[7,0,0,0,0,0]),
  record(workerB,'2027-01-03',[11,0,0,0,0,0]),
  record(workerB,'2027-01-04',[13,0,0,0,0,0])
],attendance([[workerA,'2027-01-02','已报待查'],[workerA,'2027-01-04','已确认未上班']]));
const left=result.rows[0].left;
assert.equal(left.value,0);assert.equal(left.days,1);assert.equal(left.absentDays,1);assert.equal(left.average,null);
assert.deepEqual(left.missingDates,[{date:'2027-01-02',status:'待核实'},{date:'2027-01-03',status:'无记录'}]);
assert.equal(result.rows[0].right.value,36);assert.equal(result.rows[0].right.days,4);
assert.equal(result.rows[0].difference,null);assert.equal(result.rows[0].combined,36);assert.equal(result.rows[0].complete,false);
assert.equal(result.missing.length,12);
assert.deepEqual([...new Set(result.missing.map(item=>item.date))],['2027-01-02','2027-01-03']);
assert.ok(result.missing.every(item=>item.worker.code==='A'));
''')

    def test_desktop_comparison_numeric_record_beats_stale_pending_but_unknown_product_still_flags(self):
        self.comparison_browser('''
const statuses=attendance([[workerA,'2027-01-01','已报待查'],[workerB,'2027-01-01','已报待查']]);
const records=[record(workerA,'2027-01-01',[7,'核实',0,0,0,0]),record(workerB,'2027-01-01',[5,'核实',0,0,0,0])];
const result=compare(records,statuses);
assert.equal(result.rows[0].left.value,7);assert.equal(result.rows[0].right.value,5);
assert.equal(result.rows[0].complete,true);assert.equal(result.rows[0].difference,2);
assert.equal(result.rows[0].left.days,1);assert.deepEqual(result.rows[0].left.missingDates,[]);
assert.equal(result.rows[0].days[0].left.status,'已确认');assert.equal(result.rows[0].days[0].right.status,'已确认');
assert.equal(result.rows[1].left.value,null);assert.equal(result.rows[1].complete,false);assert.equal(result.rows[1].difference,null);
assert.equal(result.missing.length,2);assert.ok(result.missing.every(item=>item.productIndex===1));
const selected=compare(records,statuses,{products:[0]});
assert.equal(selected.rows.length,1);assert.equal(selected.rows[0].product,'棉堆堆袜');
assert.equal(selected.totals.left.value,7);assert.equal(selected.totals.right.value,5);
assert.equal(selected.totals.combined,12);assert.equal(selected.totals.difference,2);assert.equal(selected.totals.complete,true);
assert.equal(selected.totals.left.average,7);assert.deepEqual(selected.missing,[]);
''')

    def test_desktop_comparison_date_universe_and_quantities_respect_group_process_year_and_range(self):
        self.comparison_browser('''
const records=[
  record(workerA,'2027-01-01',[1000,0,0,0,0,0]),
  record(workerA,'2027-01-02',[8,0,0,0,0,0]),
  record(workerB,'2027-01-02',[5,0,0,0,0,0]),
  record(workerA,'2027-01-03',[2000,0,0,0,0,0],'烤边'),
  record(workerC,'2027-01-03',[3000,0,0,0,0,0]),
  record(workerD,'2027-01-04',[4000,0,0,0,0,0]),
  record(workerB,'2027-01-05',[5000,0,0,0,0,0]),
  record(workerA,'2026-01-02',[6000,0,0,0,0,0])
];
const statuses=attendance([[workerA,'2027-01-04','已确认未上班'],[workerB,'2027-01-04','已确认未上班'],[workerC,'2027-01-03','已报待查'],[workerD,'2027-01-31','已报待查'],[workerA,'2026-01-03','已报待查']]);
const result=compare(records,statuses,{start:'2027-01-02',end:'2027-01-04',products:[0]});
assert.deepEqual(result.workers.map(worker=>worker.code),['A','B']);
assert.deepEqual(result.dates,['2027-01-02','2027-01-04']);
assert.equal(result.rows[0].left.value,8);assert.equal(result.rows[0].right.value,5);
assert.equal(result.rows[0].difference,3);assert.deepEqual(result.missing,[]);
const otherProcess=compare(records,statuses,{process:'烤边',start:'2027-01-03',end:'2027-01-04',products:[0]});
assert.deepEqual(otherProcess.workers.map(worker=>worker.code),['C','D']);
assert.deepEqual(otherProcess.dates,['2027-01-03','2027-01-04']);
assert.equal(otherProcess.rows[0].left.value,3000);assert.equal(otherProcess.rows[0].right.value,4000);
assert.equal(otherProcess.rows[0].difference,null);
assert.equal(otherProcess.missing.length,2);assert.ok(otherProcess.missing.every(item=>['C','D'].includes(item.worker.code)));
const future=compare(records,statuses,{start:'2027-02-01',end:'2027-12-31'});
assert.deepEqual(future.dates,[]);assert.deepEqual(future.missing,[]);
assert.equal(future.totals.left.value,null);assert.equal(future.totals.difference,null);
''')

    def test_desktop_comparison_totals_count_union_of_partial_numeric_days_once(self):
        self.comparison_browser('''
const result=compare([
  record(workerA,'2027-01-01',[10,'核实',0,0,0,0]),
  record(workerA,'2027-01-02',['核实',20,0,0,0,0]),
  record(workerB,'2027-01-01',[4,5,0,0,0,0]),
  record(workerB,'2027-01-02',[6,7,0,0,0,0])
],new Map(),{products:[0,1]});
assert.equal(result.rows[0].left.days,1);assert.equal(result.rows[1].left.days,1);
assert.equal(result.totals.left.value,30);assert.equal(result.totals.left.days,2);
assert.equal(result.totals.left.average,null);assert.equal(result.totals.left.complete,false);
assert.equal(result.totals.right.value,22);assert.equal(result.totals.right.days,2);assert.equal(result.totals.right.average,11);
assert.equal(result.totals.combined,52);assert.equal(result.totals.difference,null);
''')

    def test_desktop_comparison_empty_future_year_is_empty_and_invalid_ranges_clear_results(self):
        self.comparison_browser('''
const empty=compare([]);
assert.equal(empty.valid,true);assert.deepEqual(empty.dates,[]);assert.deepEqual(empty.missing,[]);
assert.equal(empty.totals.left.value,null);assert.equal(empty.totals.right.value,null);
assert.equal(empty.totals.left.days,0);assert.equal(empty.totals.left.average,null);
assert.equal(empty.totals.complete,false);assert.equal(empty.totals.combined,null);assert.equal(empty.totals.difference,null);
const records=[record(workerA,'2027-01-01',[100,0,0,0,0,0]),record(workerB,'2027-01-01',[50,0,0,0,0,0])];
for(const override of [
  {start:'2027-01-31',end:'2027-01-01'},
  {start:'2026-12-31',end:'2027-01-01'},
  {start:'2027-02-29',end:'2027-03-01'},
  {start:'2027-02-30',end:'2027-03-01'},
  {start:'2027-1-01'}, {end:''}, {products:[]}, {products:[6]}, {process:'染色'}
]){
  const result=compare(records,new Map(),override);
  assert.equal(result.valid,false,JSON.stringify(override));assert.ok(result.error);
  assert.deepEqual(result.rows,[]);assert.deepEqual(result.dates,[]);assert.deepEqual(result.missing,[]);
  assert.equal(result.totals.left.value,null);assert.equal(result.totals.right.value,null);
  assert.equal(result.totals.complete,false);assert.equal(result.totals.difference,null);
}
activeYear='2026';
const outsidePeriod=compare([],new Map(),{start:'2026-06-30',end:'2026-07-01'});
assert.equal(outsidePeriod.valid,false);
const withinPeriod=compare([],new Map(),{start:'2026-07-01',end:'2026-12-31'});
assert.equal(withinPeriod.valid,true);assert.deepEqual(withinPeriod.dates,[]);
''')

    def test_desktop_comparison_keeps_existing_mobile_status_and_card_outputs(self):
        self.comparison_browser(r'''
const records=[record(workerA,'2027-01-01',[0,12,0,0,0,0]),record(workerB,'2027-01-01',[0,8,0,0,0,0])];
const statuses=attendance([[workerA,'2027-01-01','已报待查']]);
const before=summaryCards([workerA],records,statuses,'2027-01');
const card=recordCard(workerA,record(workerA,'2027-01-02',[0,'核实',12,0,0,0]),'待核实','2027-01-02');
assert.match(before,/class="mobile-view"/);assert.match(before,/已确认累计/);
assert.match(before,/待核实 1 天：2027-01-01/);assert.match(before,/>12<small>双<\/small>/);
assert.match(card,/已确认小计/);assert.match(card,/>12<small>双<\/small>/);
assert.match(card,/class="quantity-pending">核实<\/dd>/);assert.match(card,/class="state-pending">待核实<\/span>/);
const comparison=compare(records,statuses);
assert.equal(comparison.totals.complete,true);assert.equal(comparison.totals.difference,4);
assert.equal(summaryCards([workerA],records,statuses,'2027-01'),before);
assert.equal(statusFor(statuses,'2027-01-01',workerA.person),'待核实');
''')

    def test_desktop_comparison_period_presets_validate_calendar_year_and_inclusive_boundaries(self):
        self.annual_browser('''
assert.deepEqual(comparisonPeriod('month','2027-02','','','','2027'),{valid:true,start:'2027-02-01',end:'2027-02-28'});
assert.deepEqual(comparisonPeriod('month','2027-01','','','','2027'),{valid:true,start:'2027-01-01',end:'2027-01-31'});
assert.deepEqual(comparisonPeriod('month','2026-12','','','','2026'),{valid:true,start:'2026-12-01',end:'2026-12-31'});
assert.deepEqual(comparisonPeriod('day','','2027-01-15','','','2027'),{valid:true,start:'2027-01-15',end:'2027-01-15'});
assert.deepEqual(comparisonPeriod('range','','','2026-07-01','2026-12-31','2026'),{valid:true,start:'2026-07-01',end:'2026-12-31'});
assert.deepEqual(comparisonPeriod('range','','','2027-01-01','2027-12-31','2027'),{valid:true,start:'2027-01-01',end:'2027-12-31'});
assert.deepEqual(comparisonPeriod('range','','','2027-01-31','2027-02-01','2027'),{valid:true,start:'2027-01-31',end:'2027-02-01'});
for(const args of [
  ['month','2027-13','','','','2027'], ['month','2026-10','','','','2027'],
  ['month','2026-06','','','','2026'], ['day','','2027-02-29','','','2027'],
  ['day','','2027-01-01','','','2026'], ['day','','2027-1-1','','','2027'],
  ['range','','','2027-02-01','2027-01-31','2027'],
  ['range','','','2026-12-31','2027-01-01','2027'],
  ['range','','','2026-06-30','2026-07-01','2026'],
  ['range','','','','2027-01-01','2027'], ['range','','','2027-01-01','2027-01-02','2099'],
  ['invalid','','','2027-01-01','2027-01-02','2027']
]){const result=comparisonPeriod(...args);assert.equal(result.valid,false,JSON.stringify(args));assert.ok(result.error);}
assert.equal(comparisonDateValid('2028-02-29'),true);
assert.equal(comparisonDateValid('2027-02-29'),false);
assert.equal(comparisonDateValid('2027-04-31'),false);
''')

    def test_desktop_comparison_latest_date_uses_selected_pair_and_real_records(self):
        self.comparison_browser('''
const records=[record(workerA,'2027-01-02',[10,0,0,0,0,0]),record(workerB,'2027-01-03',['核实',0,0,0,0,0]),
  record(workerC,'2027-01-07',[20,0,0,0,0,0]),record(workerD,'2027-01-06',[10,0,0,0,0,0]),
  record(workerA,'2027-02-30',[1,0,0,0,0,0]),record(workerA,'2026-12-31',[1,0,0,0,0,0]),
  record(workerA,'2027-01-08',[1,0,0,0,0,0],'烤边')];
const statuses=attendance([[workerA,'2027-12-31','待核实'],[workerB,'2027-01-20','已确认未上班']]);
assert.equal(latestComparisonDate(DEFAULT_WORKERS,records,statuses,'下机','2027'),'2027-01-03');
assert.equal(latestComparisonDate(DEFAULT_WORKERS,records,statuses,'烤边','2027'),'2027-01-07');
assert.equal(latestComparisonDate(DEFAULT_WORKERS,[],statuses,'下机','2027'),'2027-01-20');
const absent=attendance([[workerC,'2027-01-29','已确认未上班'],[workerA,'2027-01-10','已确认未上班'],[workerB,'2027-01-30','已报待查']]);
assert.equal(latestComparisonDate(DEFAULT_WORKERS,[],absent,'下机','2027'),'2027-01-10');
const summer=[record(workerA,'2026-06-30',[99,0,0,0,0,0]),record(workerA,'2026-07-01',[0,0,0,0,0,0])];
assert.equal(latestComparisonDate(DEFAULT_WORKERS,summer,new Map(),'下机','2026'),'2026-07-01');
''')

    def test_desktop_comparison_latest_default_migrates_old_cache_and_advances_with_new_data(self):
        self.desktop_comparison_browser('''
const records=[record(workerA,'2027-01-03',[10,0,0,0,0,0]),record(workerB,'2027-01-03',[5,0,0,0,0,0]),record(workerC,'2027-01-07',[20,0,0,0,0,0])];
const statuses=attendance([[workerA,'2027-12-31','待核实']]);
prepareProductionComparison(DEFAULT_WORKERS,records,statuses);
assert.equal(element('#comparison-mode').value,'latest');
assert.match(element('#comparison-result').innerHTML,/2027-01-03 至 2027-01-03/);
assert.equal(periodFields.every(field=>field.hidden),true);
assert.equal(element('#comparison-day').value,'2027-01-01');
const newer=[...records,record(workerA,'2027-01-04',[30,0,0,0,0,0])];
prepareProductionComparison(DEFAULT_WORKERS,newer,statuses);
assert.match(element('#comparison-result').innerHTML,/2027-01-04 至 2027-01-04/);
element('#comparison-process').value='烤边';renderProductionComparison(DEFAULT_WORKERS,newer,statuses);
assert.match(element('#comparison-result').innerHTML,/2027-01-07 至 2027-01-07/);
assert.equal(element('#comparison-day').value,'2027-01-01');
''', local_storage={
            'production-view-2027-comparison-mode': 'month',
            'production-view-2027-comparison-month': '2027-12',
            'production-view-2027-comparison-day': '2027-01-01',
        })

    def test_desktop_comparison_latest_preserves_new_manual_date_choices(self):
        self.desktop_comparison_browser('''
const records=[record(workerA,'2027-01-03',[10,0,0,0,0,0]),record(workerB,'2027-01-03',[5,0,0,0,0,0]),record(workerA,'2027-01-07',[20,0,0,0,0,0])];
const statuses=new Map();window.__dashboardContext={workers:DEFAULT_WORKERS,records,statusMap:statuses};
prepareProductionComparison(DEFAULT_WORKERS,records,statuses);
const changeMode=mode=>{const input=element('#comparison-mode');input.value=mode;input.matches=()=>false;element('.production-comparison').listeners.change({target:input});};
changeMode('day');element('#comparison-day').value='2027-01-03';saveView('comparison-day','2027-01-03');
renderProductionComparison(DEFAULT_WORKERS,records,statuses);
changeMode('latest');assert.match(element('#comparison-result').innerHTML,/2027-01-07 至 2027-01-07/);
changeMode('day');assert.equal(element('#comparison-day').value,'2027-01-03');
assert.match(element('#comparison-result').innerHTML,/2027-01-03 至 2027-01-03/);
assert.equal(savedView('comparison-date-mode','latest'),'day');
const newer=[...records,record(workerA,'2027-01-08',[25,0,0,0,0,0])];
prepareProductionComparison(DEFAULT_WORKERS,newer,statuses);
assert.equal(element('#comparison-mode').value,'day');assert.match(element('#comparison-result').innerHTML,/2027-01-03 至 2027-01-03/);
for(const mode of ['month','range']){saveView('comparison-date-mode',mode);prepareProductionComparison(DEFAULT_WORKERS,newer,statuses);assert.equal(element('#comparison-mode').value,mode);}
''')

    def test_desktop_comparison_preferences_and_folds_survive_snapshot_boot_and_year_switch(self):
        self.desktop_comparison_browser('''
const records=[record(workerC,'2027-01-02',[10,20,30,40,50,60]),record(workerD,'2027-01-02',[5,15,25,35,45,55])];
const previousRecords=[record(workerA,'2026-10-02',[1,2,3,4,5,6]),record(workerB,'2026-10-02',[2,3,4,5,6,7])];
const statuses=new Map();
window.__dashboardContext={workers:DEFAULT_WORKERS,records,statusMap:statuses};
prepareProductionComparison(DEFAULT_WORKERS,records,statuses);
assert.equal(element('#comparison-process').value,'烤边');assert.equal(element('#comparison-mode').value,'range');
assert.equal(element('#comparison-start').value,'2027-01-01');assert.equal(element('#comparison-end').value,'2027-01-03');
assert.deepEqual(comparisonSelectedProducts(),[0,2]);
assert.match(element('#comparison-result').innerHTML,/data-comparison-product="0" aria-expanded="true"/);
assert.match(element('#comparison-result').innerHTML,/data-comparison-product="2" aria-expanded="false"/);
assert.equal(element('#comparison-day').min,'2027-01-01');assert.equal(element('#comparison-day').max,'2027-12-31');
assert.equal(savedView('worker','all'),'C');assert.equal(savedView('summary-worker','all'),'D');
assert.equal(savedView('monthly-desktop-2027-01-02','closed'),'open');assert.equal(savedView('desktop-history-C','closed'),'open');
const toggle={dataset:{comparisonProduct:'2'},attributes:{'aria-expanded':'false','aria-controls':'comparison-days-2'},getAttribute(name){return this.attributes[name];},setAttribute(name,value){this.attributes[name]=value;},closest(selector){return selector==='[data-comparison-product]'?this:null;}};
element('.production-comparison').listeners.click({target:toggle});
assert.equal(toggle.attributes['aria-expanded'],'true');assert.equal(savedView('comparison-fold-烤边-2','closed'),'open');
prepareProductionComparison(DEFAULT_WORKERS,records,statuses);
assert.match(element('#comparison-result').innerHTML,/data-comparison-product="2" aria-expanded="true"/);
refresh=()=>{
  const yearRecords=activeYear==='2027'?records:previousRecords;
  prepareProductionComparison(DEFAULT_WORKERS,yearRecords,statuses);
};
selectDashboardYear('2026');
assert.equal(element('#comparison-process').value,'下机');assert.equal(element('#comparison-mode').value,'latest');
assert.equal(element('#comparison-month').value,'2026-10');assert.deepEqual(comparisonSelectedProducts(),[1]);
assert.equal(element('#comparison-day').min,'2026-07-01');assert.equal(element('#comparison-day').max,'2026-12-31');
assert.match(element('#comparison-result').innerHTML,/data-comparison-product="1" aria-expanded="true"/);
selectDashboardYear('2027');
assert.equal(element('#comparison-process').value,'烤边');assert.equal(element('#comparison-mode').value,'range');
assert.deepEqual(comparisonSelectedProducts(),[0,2]);
assert.match(element('#comparison-result').innerHTML,/data-comparison-product="2" aria-expanded="true"/);
assert.equal(savedView('worker','all'),'C');assert.equal(savedView('desktop-history-C','closed'),'open');
''', local_storage={
            'production-view-2027-comparison-process': '烤边',
            'production-view-2027-comparison-date-mode': 'range',
            'production-view-2027-comparison-month': '2027-01',
            'production-view-2027-comparison-start': '2027-01-01',
            'production-view-2027-comparison-end': '2027-01-03',
            'production-view-2027-comparison-products': '0,2',
            'production-view-2027-comparison-fold-烤边-0': 'open',
            'production-view-2027-worker': 'C',
            'production-view-2027-summary-worker': 'D',
            'production-view-2027-monthly-desktop-2027-01-02': 'open',
            'production-view-2027-desktop-history-C': 'open',
            'production-view-2026-comparison-process': '下机',
            'production-view-2026-comparison-month': '2026-10',
            'production-view-2026-comparison-products': '1',
            'production-view-2026-comparison-fold-下机-1': 'open',
        })

    def test_desktop_comparison_renderer_marks_partial_totals_and_replaces_invalid_results(self):
        self.desktop_comparison_browser(r'''
const records=[record(workerA,'2027-01-01',[10,'核实',0,0,0,0]),record(workerB,'2027-01-01',[5,3,0,0,0,0])];
window.__dashboardContext={workers:DEFAULT_WORKERS,records,statusMap:new Map()};
prepareProductionComparison(DEFAULT_WORKERS,records,new Map());
const target=element('#comparison-result'),html=target.innerHTML;
assert.match(html,/>18<\/span><small>已确认小计 · 数据未完整<\/small>/);
assert.match(html,/— · 数据未完整/);assert.match(html,/日均 —（数据未完整）/);
assert.match(html,/id="comparison-missing"/);assert.match(html,/冰冰袜<\/td><td>待核实/);
assert.match(html,/data-comparison-product="1" aria-expanded="false"/);
assert.match(html,/id="comparison-days-1" hidden/);
assert.equal((html.match(/0 · 持平/g)||[]).length,8);
element('#comparison-mode').value='range';element('#comparison-start').value='2027-01-03';element('#comparison-end').value='2027-01-01';
renderProductionComparison(DEFAULT_WORKERS,records,new Map());
assert.match(target.innerHTML,/开始日期不能晚于结束日期/);assert.doesNotMatch(target.innerHTML,/comparison-table|comparison-days-|comparison-missing/);
element('#comparison-start').value='2027-01-01';element('#comparison-end').value='2027-01-03';
productInputs.forEach(input=>input.checked=false);
renderProductionComparison(DEFAULT_WORKERS,records,new Map());
assert.match(target.innerHTML,/请至少选择一种有效产品/);assert.doesNotMatch(target.innerHTML,/comparison-table/);
const allButton={closest(selector){return selector==='#comparison-all'?this:null;}};
element('.production-comparison').listeners.click({target:allButton});
assert.deepEqual(comparisonSelectedProducts(),[0,1,2,3,4,5]);assert.equal(savedView('comparison-products','none'),'0,1,2,3,4,5');
assert.match(target.innerHTML,/comparison-table/);
''')

    def test_desktop_comparison_does_not_prepare_render_or_bind_on_mobile(self):
        self.comparison_browser('''
window.matchMedia=()=>({matches:false});
const document={querySelector(){throw Error('Mobile comparison touched the DOM');},querySelectorAll(){throw Error('Mobile comparison touched the DOM');}};
prepareProductionComparison(DEFAULT_WORKERS,[],new Map());
renderProductionComparison(DEFAULT_WORKERS,[],new Map());
bindProductionComparison();
assert.equal(comparisonEventController,null);assert.equal(comparisonPreparedYear,null);
''')

    def test_desktop_comparison_empty_period_has_clear_message_without_product_or_missing_table(self):
        self.desktop_comparison_browser('''
const records=[record(workerA,'2027-01-01',[10,0,0,0,0,0]),record(workerB,'2027-01-01',[5,0,0,0,0,0])];
prepareProductionComparison(DEFAULT_WORKERS,records,new Map());
assert.match(element('#comparison-result').innerHTML,/comparison-table/);
element('#comparison-mode').value='range';element('#comparison-start').value='2027-02-01';element('#comparison-end').value='2027-12-31';
renderProductionComparison(DEFAULT_WORKERS,records,new Map());
const html=element('#comparison-result').innerHTML;
assert.match(html,/2027-02-01 至 2027-12-31（含首尾）/);
assert.match(html,/该范围暂无本工序两人的记录或出勤状态/);
assert.doesNotMatch(html,/comparison-table|comparison-days-|comparison-missing|数据未完整/);
assert.equal((html.match(/有效生产 0 天/g)||[]).length,2);
''')

    def test_desktop_comparison_missing_fold_restores_per_year_and_saves_rendered_year(self):
        self.desktop_comparison_browser('''
const records2027=[record(workerA,'2027-01-02',[10,'核实',0,0,0,0]),record(workerB,'2027-01-02',[5,4,0,0,0,0])];
const records2026=[record(workerA,'2026-10-02',[10,'核实',0,0,0,0]),record(workerB,'2026-10-02',[5,4,0,0,0,0])];
prepareProductionComparison(DEFAULT_WORKERS,records2027,new Map());
assert.match(element('#comparison-result').innerHTML,/id="comparison-missing" data-year="2027" data-process="下机" open>/);
const oldDetails={id:'comparison-missing',dataset:{year:'2027',process:'下机'},open:false};
refresh=()=>prepareProductionComparison(DEFAULT_WORKERS,activeYear==='2027'?records2027:records2026,new Map());
selectDashboardYear('2026');
assert.match(element('#comparison-result').innerHTML,/id="comparison-missing" data-year="2026" data-process="下机">/);
element('.production-comparison').listeners.toggle({target:oldDetails});
assert.equal(localStorage.getItem('production-view-2027-comparison-missing-下机'),'closed');
assert.equal(localStorage.getItem('production-view-2026-comparison-missing-下机'),'closed');
const missingLink={closest(selector){return selector==='[data-comparison-missing]'?this:null;}};
const currentDetails=element('#comparison-missing');currentDetails.dataset={year:'2026',process:'下机'};currentDetails.open=false;
element('.production-comparison').listeners.click({target:missingLink});
assert.equal(currentDetails.open,true);assert.equal(localStorage.getItem('production-view-2026-comparison-missing-下机'),'open');
selectDashboardYear('2027');
assert.match(element('#comparison-result').innerHTML,/id="comparison-missing" data-year="2027" data-process="下机">/);
currentDetails.dataset={year:'2027',process:'下机'};currentDetails.open=false;
element('.production-comparison').listeners.click({target:missingLink});
prepareProductionComparison(DEFAULT_WORKERS,records2027,new Map());
assert.match(element('#comparison-result').innerHTML,/id="comparison-missing" data-year="2027" data-process="下机" open>/);
selectDashboardYear('2026');
assert.match(element('#comparison-result').innerHTML,/id="comparison-missing" data-year="2026" data-process="下机" open>/);
selectDashboardYear('2027');
assert.match(element('#comparison-result').innerHTML,/id="comparison-missing" data-year="2027" data-process="下机" open>/);
''', local_storage={
            'production-view-2027-comparison-month': '2027-01',
            'production-view-2027-comparison-missing-下机': 'open',
            'production-view-2026-comparison-month': '2026-10',
            'production-view-2026-comparison-missing-下机': 'closed',
        })

    def test_year_link_precedence_and_empty_future_year_months(self):
        self.annual_browser('''
assert.equal(activeYear,'2027');
assert.equal(chooseYear(['2026','2027'],'https://example.test/?year=2026','2027',2027),'2026');
assert.equal(chooseYear(['2026','2027'],'https://example.test/?year=2099','2027',2026),'2027');
assert.equal(chooseYear(['2026','2027'],'https://example.test/',null,2026),'2026');
assert.equal(chooseYear(['2026','2027'],'https://example.test/',null,2027),'2027');
assert.deepEqual(monthsForYear('2027'),Array.from({length:12},(_,i)=>`2027-${String(12-i).padStart(2,'0')}`));
assert.deepEqual(monthsForYear('2026'),['2026-12','2026-11','2026-10','2026-09','2026-08','2026-07']);
assert.equal(defaultMonth('2027',[],new Map(),new Date('2026-10-06T00:00:00Z')),'2027-01');
assert.equal(defaultMonth('2027',[],new Map(),new Date('2027-04-01T00:00:00Z')),'2027-04');
assert.equal(defaultMonth('2026',[],new Map(),new Date('2026-10-06T00:00:00Z')),'2026-10');
assert.equal(parseRecords().length,0);assert.equal(parseStatusMap().size,0);
''')

    def test_cross_year_records_statuses_and_preferences_are_isolated(self):
        ledger = '''### 已确认出勤状态日期
| A｜徐超超 | 2026年10月1日 | 已确认未上班 |
| A｜徐超超 | 2027年1月2日 | 已确认未上班 |
## 每日汇总
#### 2026-10-06
| 人员 | 工序 | 棉堆堆袜 | 冰冰袜 | 小腿袜 | 过膝袜 | 女船袜 | 男船袜 | 合计 |
| A｜徐超超 | 下机 | 2200 | 1700 | 300 | 0 | 0 | 0 | 4200 |
#### 2027-01-01
| 人员 | 工序 | 棉堆堆袜 | 冰冰袜 | 小腿袜 | 过膝袜 | 女船袜 | 男船袜 | 合计 |
| A｜徐超超 | 下机 | 20 | 10 | 3 | 0 | 0 | 0 | 33 |
'''
        self.annual_browser('''
assert.deepEqual(parseRecords().map(row=>row.date),['2027-01-01']);
assert.equal(aggregate(parseRecords()).total,33);
assert.deepEqual([...parseStatusMap().keys()],['2027-01-02|A｜徐超超']);
refresh=()=>{};
saveView('worker','C');saveView('scroll','80');
selectDashboardYear('2026');assert.equal(savedView('worker','all'),'all');
assert.equal(aggregate(parseRecords()).total,4200);
saveView('worker','A');window.scrollY=90;
selectDashboardYear('2027');assert.equal(savedView('worker','all'),'C');
assert.equal(window.scrollY,120);assert.match(location.href,/year=2027/);
selectDashboardYear('2026');assert.equal(savedView('worker','all'),'A');assert.equal(window.scrollY,90);
assert.equal(localStorage.getItem('production-dashboard-year'),'2026');
assert.equal(localStorage.getItem('production-view-2026-worker'),'A');
assert.equal(localStorage.getItem('production-view-2027-worker'),'C');
''', {'2026': ledger, '2027': ledger})

    def test_mobile_month_pending_dates_stay_with_selected_month_worker_and_year(self):
        ledger = '''### 已确认出勤状态日期
| A｜徐超超 | 2026年10月1日 | 已报待查 |
| A｜徐超超 | 2026年10月2日 | 已报待查 |
| A｜徐超超 | 2027年1月1日 | 已报待查 |
| A｜徐超超 | 2027年1月2日 | 已报待查 |
| A｜徐超超 | 2027年2月2日 | 已报待查 |
| B｜梅芳 | 2027年1月3日 | 已报待查 |
## 每日汇总
#### 2026-10-01
| 人员 | 工序 | 棉堆堆袜 | 冰冰袜 | 小腿袜 | 过膝袜 | 女船袜 | 男船袜 | 合计 |
| A｜徐超超 | 下机 | 500 | 100 | 核实 | 0 | 0 | 0 | 600 |
#### 2027-01-01
| 人员 | 工序 | 棉堆堆袜 | 冰冰袜 | 小腿袜 | 过膝袜 | 女船袜 | 男船袜 | 合计 |
| A｜徐超超 | 下机 | 10 | 20 | 核实 | 0 | 0 | 0 | 30 |
#### 2027-02-01
| 人员 | 工序 | 棉堆堆袜 | 冰冰袜 | 小腿袜 | 过膝袜 | 女船袜 | 男船袜 | 合计 |
| A｜徐超超 | 下机 | 100 | 200 | 核实 | 0 | 0 | 0 | 300 |
'''
        self.annual_browser('''
const worker=DEFAULT_WORKERS[0];
const records=parseRecords(),statusMap=parseStatusMap();
const january=summaryCards([worker],records,statusMap,'2027-01');
assert.match(january,/待核实 2 天：2027-01-01、2027-01-02/);
assert.doesNotMatch(january,/2027-02-|2026-|2027-01-03/);
const february=summaryCards([worker],records,statusMap,'2027-02');
assert.match(february,/待核实 2 天：2027-02-01、2027-02-02/);
assert.doesNotMatch(february,/2027-01-|2026-/);
const annual=summaryCards([worker],records,statusMap);
assert.match(annual,/待核实 4 天：/);
for(const date of ['2027-01-01','2027-01-02','2027-02-01','2027-02-02'])assert.ok(annual.includes(date));
assert.doesNotMatch(annual,/2026-|2027-01-03/);
const capacity={innerHTML:''};
const document={querySelector(selector){assert.equal(selector,'#capacity');return capacity;}};
renderCapacity([worker],records,statusMap);
assert.match(capacity.innerHTML,/待核实 4 天/);
assert.match(capacity.innerHTML,/330 双/);
activeYear='2026';md=annualLedgers['2026'];
const previousYear=summaryCards([worker],parseRecords(),parseStatusMap(),'2026-10');
assert.match(previousYear,/待核实 2 天：2026-10-01、2026-10-02/);
assert.doesNotMatch(previousYear,/2027-/);
''', {'2026': ledger, '2027': ledger})

    def test_sync_banner_requires_evidence_and_matching_versions(self):
        template = build.TEMPLATE.read_text()
        pure = template.split('function syncDisplayState', 1)[1].split('function renderSyncBanner', 1)[0]
        code = 'const assert=require("node:assert/strict");function syncDisplayState' + pure
        code += '''
const release={workbench_version:'V1.18',rules_release_id:'2026-10-06-v1.18',source_digest:'a'.repeat(64),release_status:'正式'};
const rows=['GitHub','OpenClaw','飞书'].map(source=>({source,version:'V1.18 / S1',status:'已同步'}));
const checks='| 三端规则版本 | 一致 |';
assert.equal(snapshotSyncState(release,rows,checks).kind,'ok');
assert.equal(snapshotSyncState(release,[],checks).kind,'warn');
assert.equal(snapshotSyncState(release,rows.map(x=>({...x,version:'V1.17'})),checks).kind,'error');
assert.equal(snapshotSyncState(release,rows.map(x=>({...x,status:'上传失败'})),checks).kind,'error');
assert.equal(snapshotSyncState(release,rows.map(x=>({...x,status:'上传处理中'})),checks).kind,'warn');
assert.equal(snapshotSyncState({...release,release_status:'测试'},rows,checks).kind,'warn');
'''
        self.run_node(code)

    def test_browser_no_cache_check_update_and_network_failure(self):
        template = build.TEMPLATE.read_text()
        functions = 'function cacheBypassUrl' + template.split('function cacheBypassUrl', 1)[1].split('function renderAlerts', 1)[0]
        code = '''
const assert=require('node:assert/strict');
let websiteCheckInFlight=false,websiteVerification='checking',websiteCheckedAt=null;
const dashboardRelease={source_digest:'a'.repeat(64)};
const location={href:'https://example.test/production-dashboard/index.html?year=2027&old=1',replace(value){this.replacement=value;}};
const window={scrollY:120};const saveView=()=>{};const renderSyncBanner=()=>{};
let latest={schema:1,source_digest:'a'.repeat(64),rules_release_id:'2026-10-06-v1.18'};
let fetch=async(url,options)=>{assert.equal(options.cache,'no-store');assert.match(url,/release.json.*_sync=/);return {ok:true,json:async()=>latest};};
'''+functions+'''
(async()=>{
await checkPublishedSnapshot();assert.equal(websiteVerification,'verified');assert.ok(websiteCheckedAt);assert.equal(location.replacement,undefined);
latest={...latest,source_digest:'b'.repeat(64)};await checkPublishedSnapshot();assert.match(location.replacement,/index.html.*_sync=/);
assert.equal(new URL(location.replacement).searchParams.get('year'),'2027');
location.replacement=undefined;fetch=async()=>{throw Error('offline');};await checkPublishedSnapshot();assert.equal(websiteVerification,'failed');assert.equal(location.replacement,undefined);
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        self.run_node(code)


if __name__ == '__main__':
    unittest.main()
