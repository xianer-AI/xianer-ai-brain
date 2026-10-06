"""Read-only preview of missing-field supplementation; never writes or sends."""
import re
PRODUCTS=('棉堆堆袜','冰冰袜','小腿袜','过膝袜','女船袜','男船袜')
def preview(text, worker, day, supplied):
    if worker not in 'ABCD' or not re.fullmatch(r'20\d{2}-\d{2}-\d{2}',day):
        raise ValueError('必须指定员工和生产日期')
    blocks=re.findall(r'^#### '+re.escape(day)+r'\s*\n(.*?)(?=\n#{2,}|\Z)',text,re.M|re.S)
    rows=[]
    for block in blocks:
        for line in block.splitlines():
            cells=[x.strip() for x in line.strip().strip('|').split('|')]
            if len(cells) in (9,10) and cells[0].startswith(worker+'｜'): rows.append(cells)
    if len(rows)!=1: raise ValueError('原记录不能唯一确定，禁止合并')
    original=rows[0][2:8]; values={}
    missing={p for p,v in zip(PRODUCTS,original) if v=='核实'}
    if set(supplied)!=missing: raise ValueError('只能补齐原记录待核实项，不能改动已有数量')
    for p,v in zip(PRODUCTS,original):
        n=supplied[p] if p in missing else int(v)
        if type(n) is not int or n<0: raise ValueError('数量必须为非负整数')
        values[p]=n
    return {'worker':worker,'production_date':day,'values':values,'total':sum(values.values()),'operation':'补齐原记录缺项，不新增批次','original':original}

def apply_missing(text, worker, day, supplied, source, confirmation):
    """Append only absent product details inside the existing date block."""
    data=preview(text,worker,day,supplied)
    headings=list(re.finditer(r'^### '+re.escape(day)+r'｜[^\n]+$',text,re.M))
    matching=[h for h in headings if ('徐超超','梅芳','李鸿玉','张小翠')['ABCD'.index(worker)] in h.group()]
    if len(matching)!=1: raise ValueError('个人日期记录不能唯一确定')
    h=matching[0]; next_h=re.search(r'^#{2,3} ',text[h.end():],re.M)
    end=h.end()+next_h.start() if next_h else len(text)
    body=text[h.end():end]
    for p in supplied:
        if re.search(r'^\| 20\d{6}-'+worker+r'-\d{3} \| '+re.escape(p)+r' \|',body,re.M):
            raise ValueError('目标产品已有明细，拒绝重复补齐')
    ids=[int(x) for x in re.findall(r'20\d{6}-'+worker+r'-(\d{3})',text)]
    n=max(ids,default=0)+1
    rows=''.join(f'| {day.replace("-","")}-{worker}-{n+i:03d} | {p} | {supplied[p]} | 已确认（历史缺项补齐） |\n' for i,p in enumerate(supplied))
    last=list(re.finditer(r'^\| 20\d{6}-'+worker+r'-\d{3}[^\n]+\n',body,re.M))
    if not last: raise ValueError('原明细不存在')
    pos=h.end()+last[-1].end()
    after=text[:pos]+rows+text[pos:]
    pos+=len(rows)
    after=after[:pos]+f'\n历史缺项补齐来源：{source}；确认消息：{confirmation}\n'+after[pos:]
    return after,data

def merge_extracted(text, extracted, identity):
    """Bind parsed supplement to sender identity and an exact ledger row."""
    worker=extracted.get('worker')
    if not identity or worker!=identity.get('worker'):
        raise ValueError('补核员工与发送人身份不一致')
    items=extracted.get('items') or []
    supplied={i['product']:i['quantity'] for i in items}
    if len(supplied)!=len(items): raise ValueError('补核产品重复')
    merged=preview(text,worker,extracted.get('production_date',''),supplied)
    result=dict(extracted)
    result.update(historical_supplement=supplied,original_values=merged['original'],
                  items=[{'product':p,'quantity':v,'process':'下机' if worker in 'AB' else '烤边'} for p,v in merged['values'].items()],
                  missing=[],total=merged['total'],backfill=False)
    return result

def require_bound_supplement(record):
    """Reject explicit-date partial reports at every card/write boundary."""
    import json
    result=record.get('result') or {}
    if isinstance(result,str): result=json.loads(result)
    event=record.get('event') or {}
    if isinstance(event,str): event=json.loads(event)
    extracted=result.get('extracted') or {}
    if extracted.get('status_only') or extracted.get('attendance_status'):
        return
    items=extracted.get('items') or []
    products={i.get('product') for i in items}
    explicit=re.findall(r'(20\d{2})[-年/](\d{1,2})[-月/](\d{1,2})',str(event.get('content','')))
    if len(set(explicit))>1:
        raise ValueError('报数包含多个生产日期，暂停核对，禁止自动选择日期')
    if explicit and products and products!=set(PRODUCTS):
        raise ValueError('带日期的缺项报数必须先合并原台账，禁止缺项自动补零')
