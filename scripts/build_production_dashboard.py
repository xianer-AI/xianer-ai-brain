"""Build one verifiable dashboard snapshot for desktop and mobile browsers."""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / 'docs/dashboard.template.html'
LEDGER = ROOT / '袜子生产制造袜子厂/库存记录/2026下半年下机白胚半成品统计.md'
VERSION = ROOT / 'scripts/production-parallel/VERSION.json'


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def script_json(value: object) -> str:
    # Ledger text is data even if a message happens to contain an HTML end tag.
    return json.dumps(value, ensure_ascii=False, separators=(',', ':')).replace('<', '\\u003c')


def render_snapshot(template: str, ledger: str, version: dict, *, generated_at: str | None = None,
                    builder_hash: str = '') -> tuple[bytes, bytes, dict]:
    if template.count('<script>') != 1:
        raise ValueError('dashboard template must have exactly one script injection point')
    required = ('workbench_version', 'sync_protocol_version', 'card_protocol_version')
    if any(not version.get(field) for field in required):
        raise ValueError('formal rules version is incomplete')
    release_id = version.get('release_id') or version.get('github_commit')
    if not release_id:
        raise ValueError('formal rules release identifier is missing')
    source = {
        'template_sha256': sha256(template.encode('utf-8')),
        'ledger_sha256': sha256(ledger.encode('utf-8')),
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
        'generated_at': generated_at or datetime.now(timezone.utc).isoformat(timespec='seconds'),
    }
    bootstrap = f'<script>window.__LEDGER__={script_json(ledger)};window.__DASHBOARD_RELEASE__={script_json(release)};'
    html = template.replace('<script>', bootstrap, 1).encode('utf-8')
    manifest = (json.dumps(release, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode('utf-8')
    return html, manifest, release


def capture_snapshot() -> tuple[bytes, bytes, dict]:
    # Hash the exact bytes read into this snapshot; do not hash and reread later.
    return render_snapshot(TEMPLATE.read_text(encoding='utf-8'), LEDGER.read_text(encoding='utf-8'),
                           json.loads(VERSION.read_text(encoding='utf-8')),
                           builder_hash=sha256(Path(__file__).read_bytes()))


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
