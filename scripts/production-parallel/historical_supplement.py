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
