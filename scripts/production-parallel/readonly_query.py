"""Recognize explicit read-only production questions before quantity parsing.

Keep these three expressions aligned with openclaw-gate.mjs. Deliberately
require a query prefix plus a production topic, and leave explicit report or
correction messages on the existing path.
"""
import re

QUERY_PREFIX = re.compile(r'^\s*(?:小文[，,\s]*)?(?:请|麻烦)?(?:帮我)?(?:查一下|查询|查看|查下|看一下|看下)')
QUERY_TOPIC = re.compile(r'(?:产量|产能|记录|入账|数量|总量|报表|汇总|统计)')
WRITE_INTENT = re.compile(r'(?:报数|补报|上报|更正|纠正|撤销)')


def is_readonly_production_query(content):
    text = str(content or '')
    return bool(QUERY_PREFIX.search(text) and QUERY_TOPIC.search(text) and not WRITE_INTENT.search(text))


def readonly_query_result():
    return {'agent': '规则化只读查询分流', 'draft_only': True,
            'extracted': {'kind': 'other', 'worker': 'unknown',
                          'production_date': None, 'items': [], 'missing': [],
                          'notes': ['明确只读生产查询，不生成报数核对卡或上传任务']}}
