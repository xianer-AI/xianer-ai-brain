"""Build one verifiable dashboard snapshot for desktop and mobile browsers."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / 'docs/dashboard.template.html'
LEDGER = ROOT / '袜子生产制造袜子厂/库存记录/2026下半年下机白胚半成品统计.md'
LEDGERS = {
    '2026': LEDGER,
    '2027': ROOT / '袜子生产制造袜子厂/库存记录/2027全年下机白胚半成品统计.md',
}
VERSION = ROOT / 'scripts/production-parallel/VERSION.json'


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def script_json(value: object) -> str:
    # Ledger text is data even if a message happens to contain an HTML end tag.
    return json.dumps(value, ensure_ascii=False, separators=(',', ':')).replace('<', '\\u003c')


def render_snapshot(template: str, ledger: str, version: dict, *, generated_at: str | None = None,
                    builder_hash: str = '', ledgers: dict[str, str] | None = None) -> tuple[bytes, bytes, dict]:
    if template.count('<script>') != 1:
        raise ValueError('dashboard template must have exactly one script injection point')
    required = ('workbench_version', 'sync_protocol_version', 'card_protocol_version')
    if any(not version.get(field) for field in required):
        raise ValueError('formal rules version is incomplete')
    release_id = version.get('release_id') or version.get('github_commit')
    if not release_id:
        raise ValueError('formal rules release identifier is missing')
    annual_ledgers = {'2026': ledger} if ledgers is None else dict(ledgers)
    if annual_ledgers.get('2026') != ledger:
        raise ValueError('legacy 2026 ledger must match the annual snapshot')
    if any(not year.isdigit() or len(year) != 4 or not text.strip()
           for year, text in annual_ledgers.items()):
        raise ValueError('annual ledger snapshot is incomplete')
    source = {
        'template_sha256': sha256(template.encode('utf-8')),
        'ledger_sha256': sha256(ledger.encode('utf-8')),
        'ledgers_sha256': {year: sha256(text.encode('utf-8'))
                           for year, text in sorted(annual_ledgers.items())},
        'version_sha256': sha256(json.dumps(version, sort_keys=True, ensure_ascii=False).encode('utf-8')),
        'builder_sha256': builder_hash,
    }
    release = {
        'schema': 1,
        'source_digest': sha256(json.dumps(source, sort_keys=True).encode('utf-8')),
        **source,
        **{field: version[field] for field in required},
        'rules_release_id': release_id,
        'release_status': version.get('release_status', '正式'),
        'available_years': sorted(annual_ledgers),
        'generated_at': generated_at or datetime.now(timezone.utc).isoformat(timespec='seconds'),
    }
    bootstrap = (f'<script>window.__LEDGER__={script_json(ledger)};'
                 f'window.__LEDGERS__={script_json(annual_ledgers)};'
                 f'window.__DASHBOARD_RELEASE__={script_json(release)};')
    html = template.replace('<script>', bootstrap, 1).encode('utf-8')
    manifest = (json.dumps(release, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode('utf-8')
    return html, manifest, release


def capture_snapshot() -> tuple[bytes, bytes, dict]:
    # Hash the exact bytes read into this snapshot; do not hash and reread later.
    ledgers = {year: path.read_text(encoding='utf-8') for year, path in LEDGERS.items()}
    # A generated page must never publish an unchecked accounting snapshot.
    guard_directory = ROOT / '袜子生产制造袜子厂'
    if str(guard_directory) not in sys.path:
        sys.path.insert(0, str(guard_directory))
    from production_summary_guard import check_daily_summary
    from guard_production_ledger import check
    for text in ledgers.values():
        check_daily_summary(text)
        check(text, text)
    return render_snapshot(TEMPLATE.read_text(encoding='utf-8'), ledgers['2026'],
                           json.loads(VERSION.read_text(encoding='utf-8')),
                           builder_hash=sha256(Path(__file__).read_bytes()), ledgers=ledgers)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'docs')
    args = parser.parse_args()
    html, manifest, _ = capture_snapshot()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / 'index.html').write_bytes(html)
    (args.output_dir / 'release.json').write_bytes(manifest)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
