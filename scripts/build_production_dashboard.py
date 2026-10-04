from pathlib import Path
import json

root = Path(__file__).resolve().parents[1]
ledger = root / '袜子生产制造袜子厂' / '库存记录' / '2026下半年下机白胚半成品统计.md'
template = root / 'docs' / 'dashboard.template.html'
out = root / 'docs' / 'index.html'
text = template.read_text(encoding='utf-8')
data = json.dumps(ledger.read_text(encoding='utf-8'), ensure_ascii=False)
needle = '<script>'
text = text.replace(needle, f'<script>window.__LEDGER__={data};', 1)
out.write_text(text, encoding='utf-8')
